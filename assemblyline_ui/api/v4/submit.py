import json
import os
import shutil
import tempfile

from flask import request

from assemblyline.common.dict_utils import flatten
from assemblyline.common.file import make_uri_file
from assemblyline.common.str_utils import safe_str
from assemblyline.common.uid import get_random_id
from assemblyline.odm.messages.submission import Submission
from assemblyline.odm.models.user import ROLES
from assemblyline_core.submission_client import SubmissionClient, SubmissionException
from assemblyline_ui.api.base import api_login, make_api_response, make_subapi_blueprint
from assemblyline_ui.config import ARCHIVESTORE, STORAGE, TEMP_SUBMIT_DIR, FILESTORE, config, \
    CLASSIFICATION as Classification, IDENTIFY
from assemblyline_ui.helper.service import ui_to_submission_params
from assemblyline_ui.helper.submission import download_from_url, FileTooBigException, submission_received, refang_url
from assemblyline_ui.helper.user import check_submission_quota, decrement_submission_quota, load_user_settings

SUB_API = 'submit'
submit_api = make_subapi_blueprint(SUB_API, api_version=4)
submit_api._doc = "Submit files to the system"


# Since everything the submission client needs is already being initialized
# at the global scope, we can create the submission client object at that scope as well
submission_client = SubmissionClient(datastore=STORAGE, filestore=FILESTORE, config=config, identify=IDENTIFY)


# noinspection PyUnusedLocal
@submit_api.route("/dynamic/<sha256>/", methods=["GET"])
@api_login(allow_readonly=False, require_role=[ROLES.submission_create])
def resubmit_for_dynamic(sha256, *args, **kwargs):
    """
    Resubmit a file for dynamic analysis

    Variables:
    sha256         => Resource locator (SHA256)

    Arguments (Optional):
    copy_sid    => Mimic the attributes of this SID.
    name        => Name of the file for the submission

    Data Block:
    None

    Result example:
    # Submission message object as a json dictionary
    """
    user = kwargs['user']
    quota_error = check_submission_quota(user)
    if quota_error:
        return make_api_response("", quota_error, 503)

    file_info = STORAGE.file.get(sha256, as_obj=False)
    if not file_info:
        return make_api_response({}, f"File {sha256} cannot be found on the server therefore it cannot be resubmitted.",
                                 status_code=404)

    if not Classification.is_accessible(user['classification'], file_info['classification']):
        return make_api_response("", "You are not allowed to re-submit a file that you don't have access to", 403)

    submit_result = None
    metadata = {}
    try:
        copy_sid = request.args.get('copy_sid', None)
        if copy_sid:
            submission = STORAGE.submission.get(copy_sid, as_obj=False)
        else:
            submission = None

        if submission:
            if not Classification.is_accessible(user['classification'], submission['classification']):
                return make_api_response("",
                                         "You are not allowed to re-submit a submission that you don't have access to",
                                         403)

            submission_params = submission['params']
            submission_params['classification'] = submission['classification']
            expiry = submission['expiry_ts']
            metadata = submission['metadata']

        else:
            submission_params = ui_to_submission_params(load_user_settings(user))
            submission_params['classification'] = file_info['classification']
            expiry = file_info['expiry_ts']

            # Ignore external sources
            submission_params.pop('default_external_sources', None)

        if not FILESTORE.exists(sha256):
            if ARCHIVESTORE and ARCHIVESTORE != FILESTORE and \
                    ROLES.archive_download in user['roles'] and ARCHIVESTORE.exists(sha256):

                # File exists in the archivestore, copying it to the filestore
                with tempfile.NamedTemporaryFile() as buf:
                    ARCHIVESTORE.download(sha256, buf.name)
                    FILESTORE.upload(buf.name, sha256, location='far')

            else:
                return make_api_response({}, "File %s cannot be found on the server therefore it cannot be resubmitted."
                                         % sha256, status_code=404)

        if (file_info["type"].startswith("uri/") and "uri_info" in file_info and "uri" in file_info["uri_info"]):
            name = safe_str(file_info["uri_info"]["uri"])
            submission_params['description'] = f"Resubmit {file_info['uri_info']['uri']} for Dynamic Analysis"
        else:
            name = safe_str(request.args.get('name', sha256))
            submission_params['description'] = f"Resubmit {name} for Dynamic Analysis"

        files = [{'name': name, 'sha256': sha256, 'size': file_info['size']}]

        submission_params['submitter'] = user['uname']
        submission_params['quota_item'] = True
        if 'priority' not in submission_params:
            submission_params['priority'] = 500
        if "Dynamic Analysis" not in submission_params['services']['selected']:
            submission_params['services']['selected'].append("Dynamic Analysis")

        try:
            submission_obj = Submission({
                "files": files,
                "params": submission_params,
                "metadata": metadata,
            })
        except (ValueError, KeyError) as e:
            return make_api_response("", err=str(e), status_code=400)

        submit_result = submission_client.submit(submission_obj, expiry=expiry)
        submission_received(submission_obj)
        return make_api_response(submit_result.as_primitives())

    except SubmissionException as e:
        return make_api_response("", err=str(e), status_code=400)
    finally:
        if submit_result is None:
            decrement_submission_quota(user)


# noinspection PyUnusedLocal
@submit_api.route("/resubmit/<sid>/", methods=["GET"])
@api_login(allow_readonly=False, require_role=[ROLES.submission_create])
def resubmit_submission_for_analysis(sid, *args, **kwargs):
    """
    Resubmit a submission for analysis with the exact same parameters as before

    Variables:
    sid         => Submission ID to re-submit

    Arguments:
    None

    Data Block:
    None

    Result example:
    # Submission message object as a json dictionary
    """
    user = kwargs['user']
    quota_error = check_submission_quota(user)
    if quota_error:
        return make_api_response("", quota_error, 503)

    submit_result = None
    try:
        submission = STORAGE.submission.get(sid, as_obj=False)

        if submission:
            if not Classification.is_accessible(user['classification'], submission['classification']):
                return make_api_response("",
                                         "You are not allowed to re-submit a submission that you don't have access to",
                                         403)

            submission_params = submission['params']
            submission_params['classification'] = submission['classification']
        else:
            return make_api_response({}, "Submission %s does not exists." % sid, status_code=404)

        submission_params['submitter'] = user['uname']
        submission_params['quota_item'] = True
        submission_params['description'] = "Resubmit %s for analysis" % ", ".join([x['name']
                                                                                   for x in submission["files"]])

        try:
            submission_obj = Submission({
                "files": submission["files"],
                "metadata": submission['metadata'],
                "params": submission_params,
            })
        except (ValueError, KeyError) as e:
            return make_api_response("", err=str(e), status_code=400)

        submit_result = submission_client.submit(submission_obj, expiry=submission['expiry_ts'])
        submission_received(submission_obj)

        return make_api_response(submit_result.as_primitives())
    except SubmissionException as e:
        return make_api_response("", err=str(e), status_code=400)
    finally:
        if submit_result is None:
            decrement_submission_quota(user)


# noinspection PyBroadException
@submit_api.route("/", methods=["POST"])
@api_login(allow_readonly=False, require_role=[ROLES.submission_create])
def submit(**kwargs):
    """
    Submit a single file, sha256 or url for analysis

        Note 1:
            If you are submitting a sh256 or a URL, you must use the application/json encoding and one of
            sha256 or url parameters must be included in the data block.

        Note 2:
            If you are submitting a file directly, you have to use multipart/form-data encoding this
            was done to reduce the memory footprint and speedup file transfers
             ** Read documentation of mime multipart standard if your library does not support it**

            The multipart/form-data for sending binary has two parts:
                - The first part contains a JSON dump of the optional params and uses the name 'json'
                - The last part conatins the file binary, uses the name 'bin' and includes a filename

    Variables:
    None

    Arguments:
    None

    Data Block (SHA256 or URL):
    {
      // REQUIRED: One of the two following
      "sha256": "123...DEF",      # SHA256 hash of the file already in the datastore
      "url": "http://...",        # Url to fetch the file from

      // OPTIONAL VALUES
      "name": "file.exe",         # Name of the file to scan otherwise the sha256 or base file of the url

      "metadata": {               # Submission metadata
        "key": val,                 # Key/Value pair metadata values
      },

      "params": {                 # Submission parameters
        "key": val,                 # Key/Value pair for params that different then defaults
      },                            # Default params can be fetch at /api/v3/user/submission_params/<user>/
    }

    Data Block (Binary):

    --0b34a3c50d3c02dd804a172329a0b2aa               <-- Randomly generated boundary for this http request
    Content-Disposition: form-data; name="json"      <-- JSON data blob part (only previous optional values valid)

    {"metadata": {"hello": "world"}}
    --0b34a3c50d3c02dd804a172329a0b2aa               <-- Switch to next part, file part
    Content-Disposition: form-data; name="bin"; filename="name_of_the_file_to_scan.bin"

    <BINARY DATA OF THE FILE TO SCAN... DOES NOT NEED TO BE ENCODDED>

    --0b34a3c50d3c02dd804a172329a0b2aa--             <-- End of HTTP transmission


    Result example:
    <Submission message object as a json dictionary>
    """
    user = kwargs['user']
    out_dir = os.path.join(TEMP_SUBMIT_DIR, get_random_id())

    quota_error = check_submission_quota(user)
    if quota_error:
        return make_api_response("", quota_error, 503)

    submit_result = None
    try:
        # Get data block and binary blob
        if 'multipart/form-data' in request.content_type:
            if 'json' in request.values:
                data = json.loads(request.values['json'])
            else:
                data = {}
            binary = request.files['bin']
            name = safe_str(os.path.basename(data.get("name", binary.filename) or ""))
            sha256 = None
            url = None
        elif 'application/json' in request.content_type:
            data = request.json
            binary = None
            sha256 = data.get('sha256', None)
            url = data.get('url', None)
            if url:
                url = refang_url(url)
            name = url or safe_str(os.path.basename(data.get("name", None) or sha256 or ""))
        else:
            return make_api_response({}, "Invalid content type", 400)

        # Get default description
        default_description = f"Inspection of {'URL' if url else 'file'}: {name}"

        if not name:
            return make_api_response({}, "Filename missing", 400)

        # Create task object
        if "ui_params" in data:
            s_params = ui_to_submission_params(data['ui_params'])
        else:
            s_params = ui_to_submission_params(load_user_settings(user))

        s_params.update(data.get("params", {}))
        if 'groups' not in s_params:
            s_params['groups'] = [g for g in user['groups'] if g in s_params['classification']]

        s_params['quota_item'] = True
        s_params['submitter'] = user['uname']

        # Set max extracted/supplementary if missing from request
        s_params['max_extracted'] = s_params.get('max_extracted', config.submission.default_max_extracted)
        s_params['max_supplementary'] = s_params.get('max_supplementary', config.submission.default_max_supplementary)

        # Check if external submit is allowed
        default_external_sources = s_params.pop('default_external_sources', [])

        if not Classification.is_accessible(user['classification'], s_params['classification']):
            return make_api_response({}, "You cannot start a scan with higher "
                                     "classification then you're allowed to see", 400)

        # Prepare the output directory
        try:
            os.makedirs(out_dir)
        except Exception:
            pass
        out_file = os.path.join(out_dir, get_random_id())

        # Get the output file
        extra_meta = {}
        if not binary:
            if sha256:
                found = False
                fileinfo = STORAGE.file.get_if_exists(sha256, as_obj=False)

                if fileinfo:
                    # File exists in the DB
                    if Classification.is_accessible(user['classification'], fileinfo['classification']):
                        # User has access to the file
                        if FILESTORE.exists(sha256):
                            # File exists in the filestore
                            FILESTORE.download(sha256, out_file)
                            found = True

                        elif ARCHIVESTORE and ARCHIVESTORE != FILESTORE and \
                                ROLES.archive_download in user['roles'] and ARCHIVESTORE.exists(sha256):
                            # File exists in the archivestore
                            ARCHIVESTORE.download(sha256, out_file)
                            found = True

                        if found:
                            # Found the file, now apply its classification
                            s_params['classification'] = Classification.max_classification(s_params['classification'],
                                                                                           fileinfo['classification'])

                            if (
                                fileinfo["type"].startswith("uri/")
                                and "uri_info" in fileinfo
                                and "uri" in fileinfo["uri_info"]
                            ):
                                default_description = f"Inspection of URL: {fileinfo['uri_info']['uri']}"

                if not found and default_external_sources:
                    # File is not found still, and we have external sources
                    dl_from = None
                    available_sources = [x for x in config.submission.sha256_sources
                                         if Classification.is_accessible(user['classification'], x.classification)
                                         and x.name in default_external_sources]
                    try:
                        for source in available_sources:
                            src_url = source.url.replace(source.replace_pattern, sha256)
                            src_data = source.data.replace(source.replace_pattern, sha256) if source.data else None
                            failure_pattern = source.failure_pattern.encode('utf-8') if source.failure_pattern else None
                            dl_from = download_from_url(src_url, out_file, data=src_data, method=source.method,
                                                        headers=source.headers, proxies=source.proxies,
                                                        verify=source.verify, validate=False,
                                                        failure_pattern=failure_pattern,
                                                        ignore_size=s_params.get('ignore_size', False))
                            if dl_from is not None:
                                # Apply minimum classification for the source
                                s_params['classification'] = \
                                    Classification.max_classification(s_params['classification'],
                                                                      source.classification)
                                extra_meta['original_source'] = source.name
                                found = True
                                break
                    except FileTooBigException:
                        return make_api_response({}, "File too big to be scanned.", 400)

                    if not found:
                        # File was never found, error out
                        return make_api_response(
                            {},
                            "SHA256 does not exist in Assemblyline or any of the selected sources", 404)

                if not found:
                    # File was never found, error out
                    return make_api_response({}, "SHA256 does not exist in Assemblyline", 404)
            elif url:
                if not config.ui.allow_url_submissions:
                    return make_api_response({}, "URL submissions are disabled in this system", 400)

                with tempfile.TemporaryDirectory() as dir_path:
                    shutil.move(make_uri_file(dir_path, url), out_file)
            else:
                return make_api_response({}, "Missing file to scan. No binary, sha256 or url provided.", 400)
        else:
            with open(out_file, "wb") as my_file:
                my_file.write(binary.read())

        if not s_params['description']:
            s_params['description'] = default_description

        try:
            metadata = flatten(data.get('metadata', {}))
            metadata.update(extra_meta)

            submission_obj = Submission({
                "files": [],
                "metadata": metadata,
                "params": s_params
            })
        except (ValueError, KeyError) as e:
            return make_api_response("", err=str(e), status_code=400)

        # Submit the task to the system
        try:
            submit_result = submission_client.submit(submission_obj, local_files=[(name, out_file)])
            submission_received(submission_obj)
        except SubmissionException as e:
            return make_api_response("", err=str(e), status_code=400)

        return make_api_response(submit_result.as_primitives())

    finally:
        if submit_result is None:
            decrement_submission_quota(user)

        try:
            # noinspection PyUnboundLocalVariable
            os.unlink(out_file)
        except Exception:
            pass

        try:
            shutil.rmtree(out_dir, ignore_errors=True)
        except Exception:
            pass
