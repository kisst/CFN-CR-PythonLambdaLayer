"""
Lambda function to create a Lambda layer from CFN
"""
from __future__ import print_function
import subprocess
import sys
import os
import zipfile
import importlib
import base64
try:
    import cfnresponse
    CFN_CALL = True
except ImportError:
    CFN_CALL = False

PKG_DIR = "/tmp/packages/python/lib/python3.7/site-packages"
PKG_ROOT = "/tmp/packages"


def lambda_handler(event, context):
    """
    Main function to be called by Lambda
    """
    request_type = event['RequestType']
    resource_properties = event['ResourceProperties']

    name = resource_properties['Name']
    region = resource_properties['Region']

    if request_type in ('Create', 'Update'):
        pass
    else:
        exit_gracefully("Nothing to do here", event, context)

    job_to_do = False
    try:
        install_with_pip(resource_properties['requirements'])
        job_to_do = True
    except KeyError:
        pass

    try:
        dump_text_to_file(
            resource_properties['filename'],
            resource_properties['filecontent'],
            PKG_DIR
        )
        job_to_do = True
    except KeyError:
        pass

    if job_to_do:
        zipit(PKG_ROOT, "/tmp/layer")
        layer_arn = publish_layer(name, region)
        if CFN_CALL:
            data = {"Arn": layer_arn}
            physical_id = layer_arn
            cfnresponse.send(event, context, cfnresponse.SUCCESS, data, physical_id)
    else:
        exit_gracefully('Exit! No requirements or filename/filecontent', event, context)


def exit_gracefully(message, event, context):
    """
    Echo out the message given and if called from CFN, then notify
    """
    print(message)
    # Tell CFN that do are done doing nothing
    if CFN_CALL:
        cfnresponse.send(event, context, cfnresponse.SUCCESS, {}, '')
    # Exit at this point
    sys.exit # pylint: disable=W0104


def dump_text_to_file(filename, text, dirpath):
    """
    Save extra functions text into file within the layer.
    """
    # dump variable's contents to a file under dirpath
    abs_dirpath = os.path.abspath(f"{dirpath}/lambdalayer")
    try:
        os.makedirs(abs_dirpath)
    except FileExistsError:
        pass
    abs_initpath = os.path.abspath(os.path.join(abs_dirpath, '__init__.py'))
    with open(abs_initpath, mode='a'):
        os.utime(abs_initpath, None)
    abs_filepath = os.path.abspath(os.path.join(abs_dirpath, filename))
    with open(abs_filepath, mode='w', encoding='utf-8') as file_var:
        file_var.write(base64.b64decode(text).decode('utf-8'))


def zipit(src, dst):
    """
    Create a zip file from src into dst.zip
    """
    zipf = zipfile.ZipFile("%s.zip" % (dst), "w", zipfile.ZIP_DEFLATED)
    abs_src = os.path.abspath(src)
    for dirname, _, files in os.walk(src):
        for filename in files:
            absname = os.path.abspath(os.path.join(dirname, filename))
            arcname = absname[len(abs_src) + 1:]
            zipf.write(absname, arcname)
    zipf.close()


def install_with_pip(packages):
    """
    Install pip package into /tmp folder
    """
    print(" -- Installing pip packages")
    logfile = open("/tmp/pip-install.log", "wb")
    for package in packages:
        print(" ---- Installing {}".format(package))
        subprocess.check_call([
            sys.executable, '-m', 'pip', 'install',
            '--upgrade', '-t', PKG_DIR, package], stdout=logfile)


def publish_layer(name, region):
    """
    Publish the built zip as a Lambda layer
    """
    logfile = open("/tmp/pip-install.log", "wb")
    subprocess.check_call([
        sys.executable, '-m', 'pip', 'install',
        '--upgrade', '-t', '/tmp/upload', 'boto3'], stdout=logfile)

    # my pip location
    sys.path.insert(0, '/tmp/upload')
    import botocore
    importlib.reload(botocore)
    import boto3

    client = boto3.client('lambda', region_name=region)
    response = client.publish_layer_version(
        LayerName=name,
        Description='Build with CFN Custom Resource',
        Content={'ZipFile': file_get_content('/tmp/layer.zip')},
        CompatibleRuntimes=['python3.7'])
    return response['LayerVersionArn']


def file_get_content(filename):
    """
    Read the ZIP into python parsable var
    """
    with open(filename, 'rb') as filevar:
        return filevar.read()
