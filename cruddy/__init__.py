# Copyright (c) 2016 CloudNative, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging
import uuid
import time
import decimal
import base64
import copy
import re

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

LOG = logging.getLogger()
LOG.setLevel(logging.INFO)


class CruddyKeySchemaException(Exception):

    pass


class CruddyKeyNameException(Exception):

    pass


class CRUDResponse(object):

    def __init__(self, debug=False):
        self._debug = debug
        self.status = 'success'
        self.data = None
        self.error_type = None
        self.error_code = None
        self.error_message = None
        self.raw_response = None
        self.metadata = None

    def __repr__(self):
        return 'Status: {}'.format(self.status)

    @property
    def is_successful(self):
        return self.status == 'success'

    def flatten(self):
        flat = copy.deepcopy(self.__dict__)
        hiddens = []
        for k in flat:
            if k.startswith('_'):
                hiddens.append(k)
        for k in hiddens:
            del flat[k]
        return flat

    def prepare(self):
        if self.status == 'success':
            if self.raw_response:
                if not self._debug:
                    md = self.raw_response['ResponseMetadata']
                    self.metadata = md
                    self.raw_response = None


class Tokens(object):

    token_re = re.compile('\<(?P<token>[^\s]+)\>')

    def _get_uuid(self):
        return str(uuid.uuid4())

    def _get_timestamp(self):
        return int(time.time() * 1000)

    def check(self, token):
        match = self.token_re.match(token)
        if match:
            token_method_name = '_get_{}'.format(match.group('token'))
            token_method = getattr(self, token_method_name, None)
            if callable(token_method):
                token = token_method()
        return token


class CRUD(object):

    SupportedOps = ["create", "update", "get", "delete", "list", "query"]

    def __init__(self, **kwargs):
        """
        Create a new CRUD handler.  The CRUD handler accepts the following
        parameters:

        * table_name - name of the backing DynamoDB table (required)
        * profile_name - name of the AWS credential profile to use when
          creating the boto3 Session
        * region_name - name of the AWS region to use when creating the
          boto3 Session
        * defaults - a dictionary of name/value pairs that will be used to
          initialize newly created items
        * supported_ops - a list of operations supported by the CRUD handler
          (choices are list, get, create, update, delete)
        * encrypted_attributes - a list of tuples where the first item in the
          tuple is the name of the attribute that should be encrypted and the
          second item in the tuple is the KMS master key ID to use for
          encrypting/decrypting the value
        * debug - if not False this will cause the raw_response to be left
          in the response dictionary
        """
        table_name = kwargs['table_name']
        profile_name = kwargs.get('profile_name')
        region_name = kwargs.get('region_name')
        placebo = kwargs.get('placebo')
        placebo_dir = kwargs.get('placebo_dir')
        self.defaults = kwargs.get('defaults', dict())
        self.defaults['id'] = '<uuid>'
        self.defaults['created_at'] = '<timestamp>'
        self.supported_ops = kwargs.get('supported_ops', self.SupportedOps)
        self.encrypted_attributes = kwargs.get('encrypted_attributes', list())
        self._tokens = Tokens()
        session = boto3.Session(profile_name=profile_name,
                                region_name=region_name)
        if placebo and placebo_dir:
            self.pill = placebo.attach(session, placebo_dir, debug=True)
        else:
            self.pill = None
        ddb_resource = session.resource('dynamodb')
        self.table = ddb_resource.Table(table_name)
        self._indexes = {}
        self._analyze_table()
        self._debug = kwargs.get('debug', False)
        if self.encrypted_attributes:
            self._kms_client = session.client('kms')
        else:
            self._kms_client = None

    def _analyze_table(self):
        # First check the Key Schema
        if len(self.table.key_schema) != 1:
            msg = 'cruddy does not support RANGE keys'
            raise CruddyKeySchemaException(msg)
        if self.table.key_schema[0]['AttributeName'] != 'id':
            msg = 'cruddy expects the HASH to be id'
            raise CruddyKeyNameException(msg)
        # Now process any GSI's
        if self.table.global_secondary_indexes:
            for gsi in self.table.global_secondary_indexes:
                # find HASH of GSI, that's all we support for now
                # if the GSI has a RANGE, we ignore it for now
                if len(gsi['KeySchema']) == 1:
                    gsi_hash = gsi['KeySchema'][0]['AttributeName']
                    self._indexes[gsi_hash] = gsi['IndexName']

    # Because the Boto3 DynamoDB client turns all numeric types into Decimals
    # (which is actually the right thing to do) we need to convert those
    # Decimal values back into integers or floats before serializing to JSON.

    def _replace_decimals(self, obj):
        if isinstance(obj, list):
            for i in xrange(len(obj)):
                obj[i] = self._replace_decimals(obj[i])
            return obj
        elif isinstance(obj, dict):
            for k in obj.iterkeys():
                obj[k] = self._replace_decimals(obj[k])
            return obj
        elif isinstance(obj, decimal.Decimal):
            if obj % 1 == 0:
                return int(obj)
            else:
                return float(obj)
        else:
            return obj

    def _encrypt(self, item):
        for encrypted_attr, master_key_id in self.encrypted_attributes:
            if encrypted_attr in item:
                response = self._kms_client.encrypt(
                    KeyId=master_key_id,
                    Plaintext=item[encrypted_attr])
                blob = response['CiphertextBlob']
                item[encrypted_attr] = base64.b64encode(blob)

    def _decrypt(self, item):
        for encrypted_attr, master_key_id in self.encrypted_attributes:
            if encrypted_attr in item:
                response = self._kms_client.decrypt(
                    CiphertextBlob=base64.b64decode(item[encrypted_attr]))
                item[encrypted_attr] = response['Plaintext']

    def _handle_defaults(self, item, response):
        missing = set(self.defaults.keys()) - set(item.keys())
        for key in missing:
            value = self._tokens.check(self.defaults[key])
            item[key] = value

    def _check_supported_op(self, op_name, response):
        if op_name not in self.supported_ops:
            response.status = 'error'
            response.error_type = 'UnsupportedOperation'
            response.error_message = 'Unsupported operation: {}'.format(
                op_name)
            return False
        return True

    def _call_ddb_method(self, method, kwargs, response):
        try:
            response.raw_response = method(**kwargs)
        except ClientError as e:
            LOG.debug(e)
            response.status = 'error'
            response.error_message = e.response['Error'].get('Message')
            response.error_code = e.response['Error'].get('Code')
            response.error_type = e.response['Error'].get('Type')
        except Exception as e:
            response.status = 'error'
            response.error_type = e.__class__.__name__
            response.error_code = None
            response.error_message = str(e)

    def _new_response(self):
        return CRUDResponse(self._debug)

    def query(self, query_string):
        response = self._new_response()
        if self._check_supported_op('query', response):
            if '=' not in query_string:
                response.status = 'error'
                response.error_type = 'InvalidQuery'
                response.error_message = 'Only the = operation is supported'
            else:
                key, value = query_string.split('=')
                if key not in self._indexes:
                    response.status = 'error'
                    response.error_type = 'InvalidQuery'
                    msg = 'Attribute {} is not indexed'.format(key)
                    response.error_message = msg
                else:
                    params = {'KeyConditionExpression': Key(key).eq(value),
                              'IndexName': self._indexes[key]}
                    self._call_ddb_method(self.table.query, params, response)
                    if response.status == 'success':
                        response.data = self._replace_decimals(
                            response.raw_response['Items'])
        response.prepare()
        return response

    def list(self):
        response = self._new_response()
        if self._check_supported_op('list', response):
            self._call_ddb_method(self.table.scan, {}, response)
            if response.status == 'success':
                response.data = self._replace_decimals(
                    response.raw_response['Items'])
        response.prepare()
        return response

    def get(self, id, decrypt=False):
        response = self._new_response()
        if self._check_supported_op('get', response):
            if id is None:
                response.status = 'error'
                response.error_type = 'IDRequired'
                response.error_message = 'Get requires an id'
            else:
                params = {'Key': {'id': id},
                          'ConsistentRead': True}
                self._call_ddb_method(self.table.get_item, params, response)
                if response.status == 'success':
                    if 'Item' in response.raw_response:
                        item = response.raw_response['Item']
                        if decrypt:
                            self._decrypt(item)
                        response.data = self._replace_decimals(item)
                    else:
                        response.status = 'error'
                        response.error_type = 'NotFound'
                        msg = 'Item with id ({}) not found'.format(id)
                        response.error_message = msg
        response.prepare()
        return response

    def create(self, item):
        response = self._new_response()
        if self._check_supported_op('create', response):
            self._handle_defaults(item, response)
            item['modified_at'] = item['created_at']
            self._encrypt(item)
            params = {'Item': item}
            self._call_ddb_method(self.table.put_item, params, response)
            if response.status == 'success':
                response.data = item
        response.prepare()
        return response

    def update(self, item):
        response = self._new_response()
        if self._check_supported_op('update', response):
            item['modified_at'] = self._tokens.check('<timestamp>')
            self._encrypt(item)
            params = {'Item': item}
            self._call_ddb_method(self.table.put_item, params, response)
            if response.status == 'success':
                response.data = item
        response.prepare()
        return response

    def delete(self, id):
        response = self._new_response()
        if self._check_supported_op('delete', response):
            if id is None:
                response.status = 'error'
                response.error_type = 'IDRequired'
                response.error_message = 'Delete requires an id'
            else:
                params = {'Key': {'id': id}}
                self._call_ddb_method(self.table.delete_item, params, response)
        response.prepare()
        return response

    def handler(self, item, operation):
        response = self._new_response()
        operation = operation.lower()
        self._check_supported_op(operation, response)
        if response.status == 'success':
            if operation == 'list':
                response = self.list()
            elif operation == 'get':
                response = self.get(item['id'])
            elif operation == 'create':
                response = self.create(item)
            elif operation == 'update':
                response = self.update(item)
            elif operation == 'delete':
                response = self.delete(item['id'])
            elif operation == 'query':
                response = self.query(item)
        return response
