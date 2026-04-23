"""
Lambda function to create a Lambda layer from CFN
"""

import base64
import contextlib
import logging
import os
import platform
import subprocess
import sys
import zipfile

import boto3

try:
    import cfnresponse

    CFN_CALL = True
except ImportError:
    CFN_CALL = False

# Builder's own runtime + architecture — used as the default for compatibility
# when the user doesn't declare it explicitly. `python3.14` etc; `x86_64`/`arm64`.
BUILDER_RUNTIME = f"python{sys.version_info.major}.{sys.version_info.minor}"
_ARCH_MAP = {"x86_64": "x86_64", "aarch64": "arm64"}
BUILDER_ARCHITECTURE = _ARCH_MAP.get(platform.machine(), platform.machine())

PKG_ROOT = "/tmp/packages"
PKG_DIR = f"{PKG_ROOT}/python/lib/{BUILDER_RUNTIME}/site-packages"

# Lambda pre-installs a root logger; just grab it and set the level we want.
# LOG_LEVEL env var lets operators flip to DEBUG without redeploying; anything
# unrecognised falls back to INFO rather than silencing logs.
_LEVEL_NAME = os.environ.get("LOG_LEVEL", "INFO").upper()
logger = logging.getLogger()
logger.setLevel(logging.getLevelNamesMapping().get(_LEVEL_NAME, logging.INFO))


def lambda_handler(event, context):
    """
    Main function to be called by Lambda
    """
    try:
        request_type = event["RequestType"]
        resource_properties = event["ResourceProperties"]

        name = resource_properties["Name"]
        # invoked_function_arn: arn:aws:lambda:<region>:<account>:function:<name>
        region = context.invoked_function_arn.split(":")[3]

        if request_type not in ("Create", "Update"):
            exit_gracefully("Nothing to do here", event, context)

        job_to_do = False
        try:
            install_with_pip(resource_properties["requirements"])
            job_to_do = True
        except KeyError:
            pass

        try:
            dump_text_to_file(
                resource_properties["filename"],
                resource_properties["filecontent"],
                PKG_DIR,
            )
            job_to_do = True
        except KeyError:
            pass

        if job_to_do:
            zipit(PKG_ROOT, "/tmp/layer")
            compatible_runtimes = resource_properties.get("CompatibleRuntimes", [BUILDER_RUNTIME])
            compatible_architectures = resource_properties.get(
                "CompatibleArchitectures", [BUILDER_ARCHITECTURE]
            )
            layer_arn = publish_layer(name, region, compatible_runtimes, compatible_architectures)
            if CFN_CALL:
                data = {"Arn": layer_arn}
                cfnresponse.send(event, context, cfnresponse.SUCCESS, data, layer_arn)
        else:
            exit_gracefully("Exit! No requirements or filename/filecontent", event, context)
    except Exception as err:  # pylint: disable=broad-except
        logger.exception("Unhandled error")
        if CFN_CALL:
            physical_id = event.get("PhysicalResourceId") or context.log_stream_name
            cfnresponse.send(event, context, cfnresponse.FAILED, {}, physical_id, reason=str(err))
        raise


def exit_gracefully(message, event, context):
    """
    Echo out the message given and if called from CFN, then notify
    """
    logger.info(message)
    # Echo back the existing PhysicalResourceId (or the log stream on first
    # Create) so CFN doesn't orphan a phantom resource on Delete.
    if CFN_CALL:
        physical_id = event.get("PhysicalResourceId") or context.log_stream_name
        cfnresponse.send(event, context, cfnresponse.SUCCESS, {}, physical_id)
    sys.exit(0)


def dump_text_to_file(filename, text, dirpath):
    """
    Save extra functions text into file within the layer.
    """
    abs_dirpath = os.path.abspath(f"{dirpath}/lambdalayer")
    with contextlib.suppress(FileExistsError):
        os.makedirs(abs_dirpath)
    abs_initpath = os.path.abspath(os.path.join(abs_dirpath, "__init__.py"))
    with open(abs_initpath, mode="a"):
        os.utime(abs_initpath, None)
    abs_filepath = os.path.abspath(os.path.join(abs_dirpath, filename))
    with open(abs_filepath, mode="w", encoding="utf-8") as file_var:
        file_var.write(base64.b64decode(text).decode("utf-8"))


def zipit(src, dst):
    """
    Create a zip file from src into dst.zip
    """
    with zipfile.ZipFile(f"{dst}.zip", "w", zipfile.ZIP_DEFLATED) as zipf:
        abs_src = os.path.abspath(src)
        for dirname, _, files in os.walk(src):
            for filename in files:
                absname = os.path.abspath(os.path.join(dirname, filename))
                arcname = absname[len(abs_src) + 1 :]
                zipf.write(absname, arcname)


def install_with_pip(packages):
    """
    Install pip package into /tmp folder
    """
    logger.info("Installing pip packages")
    with open("/tmp/pip-install.log", "wb") as logfile:
        for package in packages:
            logger.info("Installing %s", package)
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", "--upgrade", "-t", PKG_DIR, package],
                stdout=logfile,
            )


def publish_layer(name, region, compatible_runtimes, compatible_architectures):
    """
    Publish the built zip as a Lambda layer
    """
    client = boto3.client("lambda", region_name=region)
    response = client.publish_layer_version(
        LayerName=name,
        Description="Build with CFN Custom Resource",
        Content={"ZipFile": file_get_content("/tmp/layer.zip")},
        CompatibleRuntimes=compatible_runtimes,
        CompatibleArchitectures=compatible_architectures,
    )
    return response["LayerVersionArn"]


def file_get_content(filename):
    """
    Read the ZIP into python parsable var
    """
    with open(filename, "rb") as filevar:
        return filevar.read()
