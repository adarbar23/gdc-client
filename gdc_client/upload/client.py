from urlparse import urljoin
from multiprocessing import Pool, Manager
import requests
from exceptions import KeyError
import platform
from lxml import etree
import math
import os
import signal
import json
import sys
import yaml
from mmap import mmap, PAGESIZE
import contextlib
from progressbar import ProgressBar, Percentage, Bar
from collections import deque
import time
import copy
from ..log import get_logger
from ..defaults import part_size


OS_WINDOWS = platform.system() == 'Windows'

if not OS_WINDOWS:
    from mmap import PROT_READ
else:
    from multiprocessing import freeze_support
    # needed for forking to work
    freeze_support()


    from multiprocessing.pool import ThreadPool as Pool

    # Fake multiprocessing manager namespace
    class FakeNamespace(object):
        def __init__(self):
            self.completed = 0

    from mmap import ALLOCATIONGRANULARITY as PAGESIZE
    from mmap import ACCESS_READ


log = get_logger('upload-client')
log.propagate = False


def upload_multipart_wrapper(args):
    return upload_multipart(*args)


class Stream(object):

    def __init__(self, file, pbar, filesize):
        self._file = file
        self.pbar = pbar
        self.filesize = filesize

    def __getattr__(self, attr):
        return getattr(self._file, attr)

    def read(self, num):
        self.pbar.update(min(self.pbar.currval+num, self.filesize))
        return self._file.read(num)


def upload_multipart(filename, offset, bytes, url, upload_id, part_number,
                     headers, verify=True, pbar=None, ns=None):
    tries = 10
    while tries > 0:
        try:
            log.debug("Start upload part {}".format(part_number))
            f = open(filename, 'rb')
            if OS_WINDOWS:
                chunk_file = mmap(
                    fileno=f.fileno(),
                    length=bytes,
                    offset=offset,
                    access=ACCESS_READ
                )
            else:
                chunk_file = mmap(
                    fileno=f.fileno(),
                    length=bytes,
                    offset=offset,
                    prot=PROT_READ
                )
            res = requests.put(
                url +
                "?uploadId={}&partNumber={}".format(upload_id, part_number),
                headers=headers, data=chunk_file, verify=verify)
            chunk_file.close()
            f.close()
            if res.status_code == 200:
                if pbar:
                    pbar.fd = sys.stdout
                    ns.completed += 1
                    pbar.update(ns.completed)
                log.debug("Finish upload part {}".format(part_number))
                return True
            else:
                time.sleep(2)
                tries -= 1
                log.debug("Retry upload part {}, {}".format(part_number, res.text))

        except:
            time.sleep(2)
            tries -= 1
    return False


class GDCUploadClient(object):

    def __init__(self, token, processes, server,
                 multipart=True, debug=False, part_size=part_size,
                 files={}, verify=True, manifest_name=None):
        self.headers = {'X-Auth-Token': token}
        self.manifest_name = manifest_name
        self.verify = False if OS_WINDOWS else verify
        self.files = files
        self.incompleted = deque(copy.deepcopy(self.files))
        self.server = server
        self.multipart = multipart
        self.upload_id = None
        self.debug = debug
        self.processes = processes
        self.part_size = (part_size/PAGESIZE+1)*PAGESIZE
        self.retries = 10
        self._metadata = None
        self.resume_path = None


    @property
    def metadata(self):
        return self._metadata or self.get_metadata(self.node_id)

    def get_metadata(self, id):
        '''
        Get file's project_id and filename from graphql
        '''
        try:
            self._metadata = None
            query = {'query':
                      """query Files { node (id: "%s") 
                      { project_id, file_name }}""" %id}
            r = requests.post(
                    urljoin(self.server, "v0/submission/graphql"), 
                    headers=self.headers,
                    data=json.dumps(query),
                    verify=self.verify)
            if r.status_code == 404:
                raise Exception("File with id {} not found".format(id))
            elif r.status_code == 200:
                for node in r.json()['data']['node']:
                    self._metadata = node
                    return self._metadata
                raise Exception("File with id {} not found".format(id))
            else:
                raise Exception("Fail to get filename: {}".format(r.text))
        except Exception as e:
            raise Exception("Can't connect to gdcapi: {}".format(e.message))

    def get_file(self, f, action='download'):
        '''Parse file information from manifest'''
        try:
            self.node_id = f['id']
            project_id = f.get('project_id') or self.metadata['project_id']
            tokens = project_id.split('-')
            program = (tokens[0]).upper()
            project = ('-'.join(tokens[1:])).upper()
            if not program or not project:
                raise RuntimeError('Unable to parse project id {}'
                                   .format(project_id))
            self.url = urljoin(
                self.server, 'v0/submission/{}/{}/files/{}'
                .format(program, project, f['id']))

            if action == 'delete':
                return

            self.path = f.get('path') or '.'
            self.filename = f.get('file_name') or self.metadata['file_name']
            self.file_path = os.path.join(self.path, self.filename)                
            self.file = open(self.file_path, 'rb')

            self.file_size = os.fstat(self.file.fileno()).st_size
            self.upload_id = f.get('upload_id')
            
        except KeyError as e:
            raise KeyError(
                "Please provide {} from manifest or as an argument"
                .format(e.message))

    def called(self, arg):
        if arg:
            self.pbar.update(self.pbar.currval+1)

    def upload(self):
        '''Upload files to object storage'''
        for f in self.files:
            self.get_file(f)
            print("Attempting to upload to {}".format(self.url))
            if not self.multipart:
                self._upload()
            else:

                if self.file_size < self.part_size:
                    print "File size smaller than part size {}, do simple upload".format(self.part_size)
                    self._upload()
                else:
                    self.multipart_upload()
            self.incompleted.popleft()

    def abort(self):
        ''' Abort multipart upload'''
        for f in self.files:
            self.get_file(f)
            r = requests.delete(
                self.url+"?uploadId={}".format(self.upload_id),
                headers=self.headers, verify=self.verify)
            if r.status_code not in [204, 404]:
                raise Exception(
                    "Fail to abort multipart upload: \n{}".format(r.text))
            else:
                print "Abort multipart upload {}".format(self.upload_id)

    def delete(self):
        '''Delete file from object storage'''
        for f in self.files:
            self.get_file(f, 'delete')
            r = requests.delete(
                self.url, headers=self.headers, verify=self.verify)
            if r.status_code == 204:
                print "Delete file {}".format(self.node_id)
            else:
                print "Fail to delete file {}: {}".format(self.node_id, r.text)

    def _upload(self):
        '''Simple S3 PUT'''
        
        with open(self.file_path, 'rb') as f:
            try:
                # r = requests.put(self.url+"/_dry_run", headers=self.headers, verify=self.verify)
                # if r.status_code != 200:
                    # print "Can't upload:{}".format(r.text)
                    # return
                self.pbar = ProgressBar(
                    widgets=[Percentage(), Bar()], maxval=self.file_size).start()
                stream = Stream(f, self.pbar, self.file_size)

                r = requests.put(
                    self.url, data=stream, headers=self.headers,
                    verify=self.verify)
                if r.status_code != 200:
                    print "Upload failed {}".format(r.text)
                    return
                self.pbar.finish()
                print "Upload finished for file {}".format(self.node_id)
            except Exception as e:
                print "Upload failed {}".format(e.message)

    def multipart_upload(self):
        '''S3 Multipart upload'''
        if self.initiate():
            with self.handle_multipart():
                # wait for S3 server to create this multipart upload
                self.check_multipart()
                self.upload_parts()
                self.check_multipart()
                # try again in case some parts failed
                if self.ns.completed != self.total_parts:
                    self.upload_parts()
                self.complete()

    @contextlib.contextmanager
    def handle_multipart(self):
        try:
            yield
            self.upload_id = None
        except Exception as e:
            print "Saving unfinished upload file"
            if self.upload_id:
                self.incompleted[0]['upload_id'] = self.upload_id
            path = "resume_{}".format(self.manifest_name or self.node_id)
            with open(path, 'w') as f:
                f.write(
                    yaml.dump({"files": list(self.incompleted)},
                              default_flow_style=False))
            print 'Saved to', path
            self.resume_path = path
            if self.debug:
                raise
            else:
                print "Failure:", e.message

    def check_multipart(self):
        tries = self.retries

        while tries:
            if self.list_parts() is None:
                tries -= 1
                time.sleep(2)
            else:
                return
        raise Exception(
            "Can't find multipart upload with upload id {}"
            .format(self.upload_id))

    def initiate(self):
        if not self.upload_id:
            r = requests.post(
                self.url+"?uploads", headers=self.headers, verify=self.verify)
            if r.status_code == 200:
                xml = XMLResponse(r.text)
                self.upload_id = xml.get_key('UploadId')
                print "Start multipart upload: {}".format(self.upload_id)
                return True
            else:
                print "Fail to initiate multipart upload: {}".format(r.text)
                return False
        return True

    def upload_parts(self):
        args_list = []
        if OS_WINDOWS:
            self.ns = FakeNamespace()
        else:
            manager = Manager()
            self.ns = manager.Namespace()
            self.ns.completed = 0
        part_amount = int(math.ceil(self.file_size / float(self.part_size)))
        self.total_parts = part_amount
        self.pbar = ProgressBar(
            widgets=[Percentage(), Bar()], maxval=self.total_parts).start()
        try:
            for i in xrange(part_amount):
                offset = i * self.part_size
                remaining_bytes = self.file_size - offset
                bytes = min(remaining_bytes, self.part_size)
                if not self.multiparts.uploaded(i+1):
                    args_list.append([self.file_path, offset, bytes,
                                      self.url, self.upload_id, i+1,
                                      self.headers, self.verify,
                                      self.pbar, self.ns])
                else:
                    self.total_parts -= 1
            if self.total_parts == 0:
                return
            self.pbar.maxval = self.total_parts

            pool = Pool(processes=self.processes)
            pool.map_async(upload_multipart_wrapper, args_list).get(9999999)
            pool.close()
            pool.join()
        except KeyboardInterrupt:
            print "Caught KeyboardInterrupt, terminating workers"
            pool.terminate()
            pool.join()
            raise Exception("Process canceled by user")

    def list_parts(self):
        r = requests.get(self.url+"?uploadId={}".format(self.upload_id),
                         headers=self.headers, verify=self.verify)
        if r.status_code == 200:
            self.multiparts = Multiparts(r.text)
            return self.multiparts
        elif r.status_code in [403, 400]:
            raise Exception(r.text)
        return None

    def complete(self):
        self.check_multipart()
        if self.ns.completed != self.total_parts:
            raise Exception(
                """Multipart upload failed for file {}:
                completed parts:{}, total parts: {}, please try to resume"""
                .format(self.node_id,self.ns.completed, self.total_parts))

        self.pbar.finish()
        url = self.url+"?uploadId={}".format(self.upload_id)
        tries = self.retries
        tries = 1
        while tries > 0:
            r = requests.post(url,
                              data=self.multiparts.to_xml(),
                              headers=self.headers,
                              verify=self.verify)
            if r.status_code != 200:
                tries -= 1
                time.sleep(2)
            else:
                print "Multipart upload finished for file {}".format(self.node_id)
                return
        raise Exception("Multipart upload complete failed: {}".format(r.text))


class Multiparts(object):

    def __init__(self, xml_string):
        self.xml = XMLResponse(xml_string)
        self.parts = self.xml.parse("Part")

    def to_xml(self):
        root = etree.Element("CompleteMultipartUpload")
        for part in self.parts:
            xml_part = etree.SubElement(root, "Part")
            part_number = etree.SubElement(xml_part, "PartNumber")
            part_number.text = part['PartNumber']
            etag = etree.SubElement(xml_part, "ETag")
            etag.text = part["ETag"]
        return str(etree.tostring(root))

    def uploaded(self, part_number):
        for part in self.parts:
            if int(part['PartNumber']) == part_number:
                return True
        return False


class XMLResponse(object):

    def __init__(self, xml_string):
        self.root = etree.fromstring(str(xml_string))
        self.namespace = self.root.nsmap[None]

    def get_key(self, key):
        element = self.root.find("{%s}%s" % (self.namespace, key))
        if element is not None:
            return element.text
        return None

    def parse(self, key):
        elements = self.root.findall("{%s}%s" % (self.namespace, key))
        keys = []
        for element in elements:
            keys.append({ele.tag.split('}')[-1]: ele.text for ele in element})
        return keys
