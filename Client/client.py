# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
#                                                                           #
#   OpenBench is a chess engine testing framework by Andrew Grant.          #
#   <https://github.com/AndyGrant/OpenBench>  <andrew@grantnet.us>          #
#                                                                           #
#   OpenBench is free software: you can redistribute it and/or modify       #
#   it under the terms of the GNU General Public License as published by    #
#   the Free Software Foundation, either version 3 of the License, or       #
#   (at your option) any later version.                                     #
#                                                                           #
#   OpenBench is distributed in the hope that it will be useful,            #
#   but WITHOUT ANY WARRANTY; without even the implied warranty of          #
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the           #
#   GNU General Public License for more details.                            #
#                                                                           #
#   You should have received a copy of the GNU General Public License       #
#   along with this program.  If not, see <http://www.gnu.org/licenses/>.   #
#                                                                           #
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

import argparse
import importlib
import os
import requests
import shutil
import tempfile
import time
import traceback
import zipfile

class BadVersionException(Exception):
    def __init__(self, message='Wrong Client Version'):
        self.message = message
        super().__init__(self.message)

def url_join(*args):

    # Join a set of URL paths while maintaining the correct format
    return '/'.join([f.lstrip('/').rstrip('/') for f in args]) + '/'

# The worker's token, kept beside the client. An update of the client must never remove or overwrite it
TOKEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'worker.token')

def read_token():

    # The token this worker was given by the server, or None
    try:
        with open(TOKEN_FILE) as fin:
            return fin.read().strip() or None
    except OSError:
        return None

def save_token(token):

    # Readable by this user only, from the moment it exists
    descriptor = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, 'w') as fout:
        fout.write(token)
    try: os.chmod(TOKEN_FILE, 0o600)
    except OSError: pass

def try_forever(func, args, message, timeout=15):

    # Execute func(arg1, arg2, ...) until success

    while True:

        try:
            return func(*args)

        except BadVersionException:
            raise BadVersionException()

        except Exception as exception:
            print ('\n\n' + message)
            print ('[Note] Sleeping for %d seconds' % (timeout))
            print ('[Note] Traceback:')
            traceback.print_exc()
            print ()

        time.sleep(timeout)


def has_worker():
    try:
        import worker
        return True
    except ImportError:
        return False

def custom_help(default_help):

    print (default_help)

    if has_worker():
        import worker
        importlib.reload(worker)
        worker.run_openbench_worker(None)

    exit()

def parse_arguments():

    # The server may come from the environment. No credentials: a worker says hello and is given a token
    req_server  = 'OPENBENCH_SERVER' not in os.environ
    help_server = 'Server URL. May also be provided via OPENBENCH_SERVER environment variable'
    help_join   = 'One-time code from the Machines page, to be approved on hello'

    # Pretty formatting
    p = argparse.ArgumentParser(
        formatter_class=lambda prog:
            argparse.ArgumentDefaultsHelpFormatter(prog, max_help_position=10)
    )

    # Create and parse all arguments into a raw format
    p.add_argument('-S', '--server'             , help=help_server              , required=req_server)
    p.add_argument(      '--join'               , help=help_join                                     )
    p.add_argument(      '--clean'              , help='Force New Client'       , action='store_true')
    p.add_argument(      '--no-client-downloads', help='NEVER download a client', action='store_true')

    # Override, to possibly print worker.py's help as well as client.py's
    p.print_help = lambda: custom_help(p.format_help())

    # Replace with ENV variables if needed
    args, unknown = p.parse_known_args()
    args.server   = args.server   if args.server   else os.environ['OPENBENCH_SERVER']

    return args

def copy_client_files(client_dir, dest_dir):

    # Everything of the extracted Client except client.py, which is running, and the token, which is this worker's
    copied_files = 0
    for root, dirs, files in os.walk(client_dir):
        for file in files:
            if file not in ('client.py', 'worker.token'):
                shutil.copy2(os.path.join(root, file), os.path.join(dest_dir, file))
                copied_files += 1
    return copied_files

def download_client_files(args):

    try: # Needs no credentials
        target      = url_join(args.server, 'clientVersionRef')
        version_ref = requests.post(target).json()

    except:
        raise Exception('Unable to retrieve Client Version from OpenBench server')

    try: # Download the entire .zip for the branch / tag / commit ref
        repo_url = version_ref['client_repo_url']
        repo_ref = version_ref['client_repo_ref']
        response = requests.get(url_join(repo_url, 'archive', '%s.zip' % (repo_ref)))
        assert response.status_code == 200

    except:
        raise Exception('Unable to retrieve .zip archive from Github')

    try: # Save to .zip, extract, and copy contents

        # Save response content to a temp file
        with tempfile.TemporaryFile() as temp_zip_file:
            temp_zip_file.write(response.content)

            # Extract the zip with a temp file
            with tempfile.TemporaryDirectory() as temp_dir:
                with zipfile.ZipFile(temp_zip_file) as zip_file:
                    zip_file.extractall(temp_dir)

                # Copy all files except client.py
                extracted_roots = [
                    os.path.join(temp_dir, entry)
                    for entry in os.listdir(temp_dir)
                    if os.path.isdir(os.path.join(temp_dir, entry))
                ]
                client_dirs = [
                    os.path.join(root, 'Client')
                    for root in extracted_roots
                    if os.path.isdir(os.path.join(root, 'Client'))
                ]
                if len(client_dirs) != 1:
                    raise Exception('Unable to locate extracted Client directory in archive')

                if copy_client_files(client_dirs[0], os.getcwd()) == 0:
                    raise Exception('Client archive contained no updatable files')

    except:
        raise Exception('Unable to extract .zip archive contents')


if __name__ == '__main__':

    # Use client.py's path as the base pathway
    os.chdir(os.path.dirname(os.path.abspath(__file__)))

    args = parse_arguments()

    if args.no_client_downloads and not has_worker():
        raise Exception('Client missing, and --no-client-downloads provided')

    if args.clean or not has_worker():
        print ('[NOTE] Downloading Client...')
        try_forever(download_client_files, [args], 'Failed to download Client files')

    from client import BadVersionException

    while True:

        try:
            import worker
            importlib.reload(worker)
            worker.run_openbench_worker(args)

        except BadVersionException:

            if args.no_client_downloads:
                raise Exception('Client update requested, but --no-client-downloads provided')

            print ('[NOTE] Downloading newer version of Client...')
            try_forever(download_client_files, [args], 'Failed to download Client files')

        except KeyboardInterrupt:
            break
