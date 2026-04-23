# CFN-CR-PythonLambdaLayer

CloudFormation Custom Resource that builds Lambda layers from pip
requirements and/or inline Python source at stack deploy time — no S3
artefacts to upload first.

## Quickstart

Deploy the builder stack once per region:

```
aws cloudformation deploy \
  --template-file CFN-CR-PythonLambdaLayer.yaml \
  --stack-name cfn-cr-python-lambda-layer \
  --capabilities CAPABILITY_IAM
```

Then declare a layer in any downstream stack, importing the ServiceToken for
the target architecture (`x86_64` or `arm64`):

```yaml
MyDepsLayer:
  Type: Custom::LayerBuilder
  Properties:
    ServiceToken: !ImportValue "cfn:lambdalayer-x86-64:us-east-1:arn"
    Name: my-app-deps
    requirements: [requests, pydantic]
```

See `CFN-CR-PythonLambdaLayer-SampleUsage.yaml` for more examples.

## Custom resource properties

| Property | Required | Description |
|----------|----------|-------------|
| `ServiceToken` | yes | Builder Lambda ARN, via `Fn::ImportValue`. |
| `Name` | yes | Layer name. |
| `requirements` | one of these two | List of pip requirement strings. |
| `filename` + `filecontent` | one of these two | Inline Python module; `filecontent` is base64 (use `Fn::Base64`). |
| `CompatibleRuntimes` | no | Defaults to the builder's own runtime. |
| `CompatibleArchitectures` | no | Defaults to the builder's own arch. Override only for pure-Python layers. |

Calling the `x86_64` vs `arm64` builder is how you pick the target
architecture — C-extension wheels are installed on the builder's host arch,
so they must match the consumer function's arch.

## Limitations

Layers cap at 50 MB zipped / 250 MB unzipped; builder has 512 MB `/tmp` and a
15-min timeout. Delete is a no-op — clean up layer versions manually.

---
Contributions welcome: <https://github.com/kisst/CFN-CR-PythonLambdaLayer/issues>
