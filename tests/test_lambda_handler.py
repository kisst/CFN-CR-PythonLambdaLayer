"""
Drive lambda_handler with the four Custom::LayerBuilder resources from
CFN-CR-PythonLambdaLayer-SampleUsage.yaml, plus the no-op Delete path.

boto3 / subprocess.check_call / cfnresponse.send are all stubbed, so nothing
touches AWS or the network.
"""

import base64
import logging
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import yaml
from botocore.exceptions import ClientError

REPO_ROOT = Path(__file__).resolve().parent.parent
SAMPLE = REPO_ROOT / "CFN-CR-PythonLambdaLayer-SampleUsage.yaml"
FAKE_LAYER_ARN = "arn:aws:lambda:eu-west-1:111111111111:layer:test-layer:1"


class CfnTagLoader(yaml.SafeLoader):
    pass


def _passthrough(loader, tag_suffix, node):  # noqa: ARG001
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_mapping(node)


CfnTagLoader.add_multi_constructor("!", _passthrough)


def _resolve_intrinsics(value):
    """Do what CFN would do before the Lambda sees the event: encode Fn::Base64."""
    if isinstance(value, dict):
        if "Fn::Base64" in value and len(value) == 1:
            payload = _resolve_intrinsics(value["Fn::Base64"])
            return base64.b64encode(payload.encode("utf-8")).decode("ascii")
        return {k: _resolve_intrinsics(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_intrinsics(v) for v in value]
    return value


def _load_sample_resources():
    with SAMPLE.open() as fh:
        doc = yaml.load(fh, Loader=CfnTagLoader)
    return {
        name: _resolve_intrinsics(res["Properties"])
        for name, res in doc["Resources"].items()
        if res.get("Type") == "Custom::LayerBuilder"
    }


@pytest.fixture
def context():
    return SimpleNamespace(
        invoked_function_arn="arn:aws:lambda:eu-west-1:111111111111:function:cfn-cr-python-lambda-layer",
        log_stream_name="2026/04/23/[$LATEST]deadbeef",
    )


@pytest.fixture
def sent(monkeypatch):
    """Capture every cfnresponse.send(...) call the handler makes."""
    import cfnresponse  # stubbed below before import

    calls = []

    def fake_send(event, ctx, status, data, physical_id, reason=None):  # noqa: ARG001
        calls.append({"status": status, "data": data, "physical_id": physical_id, "reason": reason})

    monkeypatch.setattr(cfnresponse, "send", fake_send)
    return calls


@pytest.fixture(autouse=True)
def stub_aws(monkeypatch):
    """Swap boto3 and subprocess.check_call with no-ops before the handler runs."""
    import lambda_function

    fake_client = MagicMock()
    fake_client.publish_layer_version.return_value = {"LayerVersionArn": FAKE_LAYER_ARN}
    fake_boto3 = MagicMock()
    fake_boto3.client.return_value = fake_client
    monkeypatch.setattr(lambda_function, "boto3", fake_boto3)
    monkeypatch.setattr(subprocess, "check_call", MagicMock(return_value=0))
    return fake_client


def _make_event(request_type: str, properties: dict, physical_id: str | None = None) -> dict:
    event = {
        "RequestType": request_type,
        "ResourceProperties": {k: v for k, v in properties.items() if k != "ServiceToken"},
        "StackId": "arn:aws:cloudformation:eu-west-1:111111111111:stack/test/abc",
        "RequestId": "req-1",
        "LogicalResourceId": "TestResource",
        "ResponseURL": "https://example.invalid/response",
    }
    if physical_id is not None:
        event["PhysicalResourceId"] = physical_id
    return event


# ---------- cfnresponse stub, installed before lambda_function imports ----------


def _install_cfnresponse_stub():
    stub = type(sys)("cfnresponse")
    stub.SUCCESS = "SUCCESS"
    stub.FAILED = "FAILED"
    stub.send = lambda *a, **kw: None  # replaced per-test by the `sent` fixture
    sys.modules["cfnresponse"] = stub


_install_cfnresponse_stub()

SAMPLE_RESOURCES = _load_sample_resources()


# ---------- the real tests ----------


def test_sample_exposes_expected_cases():
    # Guard against the sample drifting without us noticing.
    assert set(SAMPLE_RESOURCES) == {
        "EmptyLambdaLayer",
        "Boto3LambdaLayer",
        "RequestsLambdaLayer",
        "PydanticLambdaLayer",
        "InlineCodeLambdaLayer",
        "InlineCodeWithPipPackageLambdaLayer",
        "Arm64BotoLambdaLayer",
    }


def test_empty_layer_is_a_graceful_noop(sent, context):
    import lambda_function

    event = _make_event("Create", SAMPLE_RESOURCES["EmptyLambdaLayer"])
    with pytest.raises(SystemExit) as exc:
        lambda_function.lambda_handler(event, context)
    assert exc.value.code == 0
    assert len(sent) == 1
    assert sent[0]["status"] == "SUCCESS"
    assert sent[0]["data"] == {}
    # No existing PhysicalResourceId on Create, so we fall back to log_stream_name.
    assert sent[0]["physical_id"] == context.log_stream_name


@pytest.mark.parametrize(
    "resource_name",
    [
        "Boto3LambdaLayer",
        "RequestsLambdaLayer",
        "PydanticLambdaLayer",
        "InlineCodeLambdaLayer",
        "InlineCodeWithPipPackageLambdaLayer",
    ],
)
def test_layer_build_publishes_and_signals_success(
    resource_name, sent, context, stub_aws, tmp_path, monkeypatch
):
    import lambda_function

    # Redirect PKG_DIR / PKG_ROOT into a tmp dir so dump_text_to_file and zipit
    # don't fight over /tmp/packages across parametrised runs.
    import lambda_function as _lf

    pkg_root = tmp_path / "packages"
    pkg_dir = pkg_root / "python" / "lib" / _lf.BUILDER_RUNTIME / "site-packages"
    pkg_dir.mkdir(parents=True)
    monkeypatch.setattr(lambda_function, "PKG_ROOT", str(pkg_root))
    monkeypatch.setattr(lambda_function, "PKG_DIR", str(pkg_dir))
    # zipit writes to /tmp/layer.zip; redirect by monkeypatching zipit's target.
    layer_zip = tmp_path / "layer"
    original_zipit = lambda_function.zipit
    monkeypatch.setattr(
        lambda_function, "zipit", lambda src, _dst: original_zipit(src, str(layer_zip))
    )
    # publish_layer reads that zip back; redirect file_get_content accordingly.
    monkeypatch.setattr(
        lambda_function,
        "file_get_content",
        lambda _path: (layer_zip.with_suffix(".zip")).read_bytes(),
    )

    props = SAMPLE_RESOURCES[resource_name]
    event = _make_event("Create", props)
    lambda_function.lambda_handler(event, context)

    assert len(sent) == 1
    assert sent[0]["status"] == "SUCCESS"
    assert sent[0]["data"] == {"Arn": FAKE_LAYER_ARN}
    assert sent[0]["physical_id"] == FAKE_LAYER_ARN

    # publish_layer was called against the region derived from the function ARN.
    stub_aws.publish_layer_version.assert_called_once()
    kwargs = stub_aws.publish_layer_version.call_args.kwargs
    assert kwargs["LayerName"] == props["Name"]
    # Default compatibility matches the builder's own runtime/arch.
    assert kwargs["CompatibleRuntimes"] == [lambda_function.BUILDER_RUNTIME]
    assert kwargs["CompatibleArchitectures"] == [lambda_function.BUILDER_ARCHITECTURE]

    # If the resource had inline code, make sure it landed where the example expects.
    if "filename" in props:
        written = (pkg_dir / "lambdalayer" / props["filename"]).read_text()
        expected = base64.b64decode(props["filecontent"]).decode("utf-8")
        assert written == expected


def test_compatibility_overrides_pass_through(sent, context, stub_aws, tmp_path, monkeypatch):
    """CompatibleRuntimes / CompatibleArchitectures on the CR should override
    the builder defaults and land verbatim in publish_layer_version.

    The primary way to pick arch is to call the matching builder Lambda
    (x86_64 vs arm64). CompatibleArchitectures override exists for pure-Python
    layers that are safe to advertise on both arches from one builder."""
    import lambda_function

    pkg_root = tmp_path / "packages"
    pkg_dir = pkg_root / "python" / "lib" / lambda_function.BUILDER_RUNTIME / "site-packages"
    pkg_dir.mkdir(parents=True)
    monkeypatch.setattr(lambda_function, "PKG_ROOT", str(pkg_root))
    monkeypatch.setattr(lambda_function, "PKG_DIR", str(pkg_dir))
    layer_zip = tmp_path / "layer"
    original_zipit = lambda_function.zipit
    monkeypatch.setattr(
        lambda_function, "zipit", lambda src, _dst: original_zipit(src, str(layer_zip))
    )
    monkeypatch.setattr(
        lambda_function,
        "file_get_content",
        lambda _path: layer_zip.with_suffix(".zip").read_bytes(),
    )

    props = {
        **SAMPLE_RESOURCES["Boto3LambdaLayer"],
        "CompatibleRuntimes": ["python3.12", "python3.13"],
        "CompatibleArchitectures": ["x86_64", "arm64"],
    }
    event = _make_event("Create", props)
    lambda_function.lambda_handler(event, context)

    kwargs = stub_aws.publish_layer_version.call_args.kwargs
    assert kwargs["CompatibleRuntimes"] == ["python3.12", "python3.13"]
    assert kwargs["CompatibleArchitectures"] == ["x86_64", "arm64"]


def test_delete_echoes_physical_resource_id(sent, context):
    import lambda_function

    event = _make_event("Delete", SAMPLE_RESOURCES["Boto3LambdaLayer"], physical_id=FAKE_LAYER_ARN)
    with pytest.raises(SystemExit) as exc:
        lambda_function.lambda_handler(event, context)
    assert exc.value.code == 0
    assert len(sent) == 1
    assert sent[0]["status"] == "SUCCESS"
    # Critical: must echo the existing id, not an empty string.
    assert sent[0]["physical_id"] == FAKE_LAYER_ARN


@pytest.mark.parametrize(
    ("env_value", "expected_level"),
    [
        (None, logging.INFO),  # unset → INFO
        ("DEBUG", logging.DEBUG),
        ("warning", logging.WARNING),  # case-insensitive
        ("nonsense", logging.INFO),  # typo → INFO, not silence
    ],
)
def test_log_level_env_var(env_value, expected_level, monkeypatch):
    if env_value is None:
        monkeypatch.delenv("LOG_LEVEL", raising=False)
    else:
        monkeypatch.setenv("LOG_LEVEL", env_value)
    # Force re-import so the module-level setLevel runs with our env.
    sys.modules.pop("lambda_function", None)
    import lambda_function

    assert lambda_function.logger.level == expected_level


def test_exception_signals_failed(sent, context, stub_aws):
    import lambda_function

    stub_aws.publish_layer_version.side_effect = RuntimeError("boom")

    event = _make_event("Create", SAMPLE_RESOURCES["Boto3LambdaLayer"])
    with pytest.raises(RuntimeError):
        lambda_function.lambda_handler(event, context)

    assert len(sent) == 1
    assert sent[0]["status"] == "FAILED"
    assert sent[0]["reason"] == "boom"
    assert sent[0]["physical_id"] == context.log_stream_name


# ---------- negative-path tests ----------
#
# The handler funnels every internal exception through the same except branch,
# so these tests check that each realistic failure cause surfaces as a FAILED
# cfnresponse with a reason, the exception still propagates (so Lambda records
# the invocation failed), and the PhysicalResourceId falls back correctly.


@pytest.mark.parametrize(
    "label",
    [
        "non-existent-package",  # pip: "Could not find a version..."
        "missing-system-dependency",  # pip: build fails due to missing libpq, etc.
    ],
    ids=["non_existent_pkg", "missing_system_dep"],
)
def test_pip_install_failure_signals_failed(label, sent, context, monkeypatch):
    """Any pip install failure — whether the package is missing or it needs
    system libs not present in the Lambda sandbox — should reach the handler
    as CalledProcessError and be signalled as FAILED."""
    import lambda_function

    monkeypatch.setattr(
        subprocess,
        "check_call",
        MagicMock(side_effect=subprocess.CalledProcessError(returncode=1, cmd=["pip", label])),
    )

    event = _make_event("Create", SAMPLE_RESOURCES["Boto3LambdaLayer"])
    with pytest.raises(subprocess.CalledProcessError):
        lambda_function.lambda_handler(event, context)

    assert len(sent) == 1
    assert sent[0]["status"] == "FAILED"
    assert "returned non-zero exit status 1" in sent[0]["reason"]
    assert sent[0]["physical_id"] == context.log_stream_name


def test_publish_layer_client_error_signals_failed(sent, context, stub_aws):
    """publish_layer_version can raise botocore ClientError for IAM denies,
    invalid layer names, oversize zips, etc. — all bubble up as FAILED."""
    import lambda_function

    stub_aws.publish_layer_version.side_effect = ClientError(
        error_response={"Error": {"Code": "AccessDeniedException", "Message": "not authorized"}},
        operation_name="PublishLayerVersion",
    )

    event = _make_event("Create", SAMPLE_RESOURCES["Boto3LambdaLayer"])
    with pytest.raises(ClientError):
        lambda_function.lambda_handler(event, context)

    assert len(sent) == 1
    assert sent[0]["status"] == "FAILED"
    assert "AccessDeniedException" in sent[0]["reason"]
    assert sent[0]["physical_id"] == context.log_stream_name


def test_missing_required_property_signals_failed(sent, context):
    """A malformed CFN event (e.g. Name missing) should send FAILED, not hang
    until the 1-hour custom-resource timeout."""
    import lambda_function

    props = {k: v for k, v in SAMPLE_RESOURCES["Boto3LambdaLayer"].items() if k != "Name"}
    event = _make_event("Create", props)
    with pytest.raises(KeyError):
        lambda_function.lambda_handler(event, context)

    assert len(sent) == 1
    assert sent[0]["status"] == "FAILED"
    assert "Name" in sent[0]["reason"]
    assert sent[0]["physical_id"] == context.log_stream_name


def test_malformed_base64_filecontent_signals_failed(sent, context):
    """Inline code that isn't valid base64 should fail cleanly instead of
    crashing the handler silently."""
    import lambda_function

    props = {
        "Name": "broken-inline",
        "filename": "oops.py",
        "filecontent": "!!!not-base64!!!",
    }
    event = _make_event("Create", props)
    with pytest.raises(Exception):  # noqa: B017 — binascii.Error is the expected type
        lambda_function.lambda_handler(event, context)

    assert len(sent) == 1
    assert sent[0]["status"] == "FAILED"
    assert sent[0]["physical_id"] == context.log_stream_name
