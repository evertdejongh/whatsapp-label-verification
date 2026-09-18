import base64
import json
import os
import sys
import types
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault('AWS_EC2_METADATA_DISABLED', 'true')

try:
    import boto3  # noqa: F401
except ModuleNotFoundError:
    boto3 = types.ModuleType('boto3')
    boto3.client = Mock(return_value=Mock())
    boto3.resource = Mock(return_value=Mock())
    boto3_dynamodb = types.ModuleType('boto3.dynamodb')
    boto3_conditions = types.ModuleType('boto3.dynamodb.conditions')
    boto3_conditions.Key = Mock()
    sys.modules.update({
        'boto3': boto3,
        'boto3.dynamodb': boto3_dynamodb,
        'boto3.dynamodb.conditions': boto3_conditions,
    })

from audit_dashboard import audit_dashboard_lambda as dashboard


class CatalogSaveTests(unittest.TestCase):
    def setUp(self):
        self.table = Mock()
        self.dynamodb = Mock()
        self.dynamodb.Table.return_value = self.table
        self.s3 = Mock()

        self.dynamodb_patch = patch.object(dashboard, 'dynamodb', self.dynamodb)
        self.s3_patch = patch.object(dashboard, 's3_client', self.s3)
        self.dynamodb_patch.start()
        self.s3_patch.start()
        self.addCleanup(self.dynamodb_patch.stop)
        self.addCleanup(self.s3_patch.stop)

    def save(self, pdf_url):
        form = {
            'action': ['save'],
            'spec_code': ['9a'],
            'description': ['Updated description'],
            'pdf_url': [pdf_url],
        }
        return dashboard.handle_catalog_post('token', form)

    def test_clearing_pdf_url_deletes_cached_pdf(self):
        self.table.get_item.return_value = {'Item': {
            'spec_code': '9A',
            'description': 'Old description',
            'pdf_url': 'https://example.test/spec.pdf',
            'pdf_s3_key': 'specs/9A_specsheet.pdf',
        }}

        response = self.save('')

        self.assertEqual(303, response['statusCode'])
        self.s3.delete_object.assert_called_once_with(
            Bucket=dashboard.SPEC_BUCKET_NAME,
            Key='specs/9A_specsheet.pdf',
        )
        self.s3.put_object.assert_not_called()
        self.table.put_item.assert_called_once_with(Item={
            'spec_code': '9A',
            'description': 'Updated description',
        })

    def test_unchanged_pdf_url_keeps_cached_pdf(self):
        pdf_url = 'https://example.test/spec.pdf'
        self.table.get_item.return_value = {'Item': {
            'spec_code': '9A',
            'description': 'Old description',
            'pdf_url': pdf_url,
            'pdf_s3_key': 'specs/9A_specsheet.pdf',
        }}

        response = self.save(pdf_url)

        self.assertEqual(303, response['statusCode'])
        self.s3.delete_object.assert_not_called()
        self.s3.put_object.assert_not_called()
        self.table.put_item.assert_called_once_with(Item={
            'spec_code': '9A',
            'description': 'Updated description',
            'pdf_url': pdf_url,
            'pdf_s3_key': 'specs/9A_specsheet.pdf',
        })


class ReferenceTablesTests(unittest.TestCase):
    def setUp(self):
        self.table = Mock()
        self.dynamodb = Mock()
        self.dynamodb.Table.return_value = self.table
        self.dynamodb_patch = patch.object(dashboard, 'dynamodb', self.dynamodb)
        self.dynamodb_patch.start()
        self.addCleanup(self.dynamodb_patch.stop)

    def test_save_single_key_table_writes_item(self):
        form = {
            'table': ['whatsapp-puc'],
            'action': ['save'],
            'is_new': ['1'],
            'col::PUC': ['A1030'],
            'col::GGN': ['4052852937450'],
            'extra_name': [''],
            'extra_value': [''],
        }
        response = dashboard.handle_tables_post('token', form)

        self.assertEqual(303, response['statusCode'])
        self.table.put_item.assert_called_once_with(Item={'PUC': 'A1030', 'GGN': '4052852937450'})

    def test_save_composite_key_table_includes_both_keys(self):
        form = {
            'table': ['whatsapp-variety'],
            'action': ['save'],
            'is_new': ['1'],
            'col::VarietyName': ['SUGRA35'],
            'col::Commodity': ['GR'],
            'col::VarietyCode': ['AUP'],
            'col::VarietyGroupCode': ['WS'],
            'extra_name': [''],
            'extra_value': [''],
        }
        response = dashboard.handle_tables_post('token', form)

        self.assertEqual(303, response['statusCode'])
        self.table.put_item.assert_called_once_with(Item={
            'VarietyName': 'SUGRA35',
            'Commodity': 'GR',
            'VarietyCode': 'AUP',
            'VarietyGroupCode': 'WS',
        })

    def test_save_missing_key_attribute_does_not_write(self):
        form = {
            'table': ['whatsapp-variety'],
            'action': ['save'],
            'is_new': ['1'],
            'col::VarietyName': ['SUGRA35'],
            'col::Commodity': [''],
            'extra_name': [''],
            'extra_value': [''],
        }
        response = dashboard.handle_tables_post('token', form)

        self.assertEqual(303, response['statusCode'])
        self.table.put_item.assert_not_called()

    def test_extra_field_added_when_name_and_value_present(self):
        form = {
            'table': ['whatsapp-commodity'],
            'action': ['save'],
            'is_new': ['1'],
            'col::Commodity': ['GR'],
            'col::CommodityName': ['Grapes'],
            'extra_name': ['Note'],
            'extra_value': ['manually added'],
        }
        response = dashboard.handle_tables_post('token', form)

        self.assertEqual(303, response['statusCode'])
        self.table.put_item.assert_called_once_with(Item={
            'Commodity': 'GR',
            'CommodityName': 'Grapes',
            'Note': 'manually added',
        })

    def test_delete_decodes_row_key_and_deletes(self):
        row_key = dashboard.encode_key({'PUC': 'A1030'})
        form = {
            'table': ['whatsapp-puc'],
            'action': ['delete'],
            'row_key': [row_key],
        }
        response = dashboard.handle_tables_post('token', form)

        self.assertEqual(303, response['statusCode'])
        self.table.delete_item.assert_called_once_with(Key={'PUC': 'A1030'})

    def test_unknown_table_redirects_without_touching_dynamodb(self):
        form = {'table': ['not-a-real-table'], 'action': ['delete'], 'row_key': ['x']}
        response = dashboard.handle_tables_post('token', form)

        self.assertEqual(303, response['statusCode'])
        self.dynamodb.Table.assert_not_called()


class SpecFilesTests(unittest.TestCase):
    def setUp(self):
        self.s3 = Mock()
        self.s3_patch = patch.object(dashboard, 's3_client', self.s3)
        self.s3_patch.start()
        self.addCleanup(self.s3_patch.stop)

        # save_json's invalid-JSON path re-renders the file list, which pulls
        # spec descriptions from whatsapp-spec-catalog -- mock dynamodb too so
        # that path doesn't make a real network call in tests.
        self.dynamodb = Mock()
        self.dynamodb.Table.return_value.scan.return_value = {'Items': []}
        self.dynamodb_patch = patch.object(dashboard, 'dynamodb', self.dynamodb)
        self.dynamodb_patch.start()
        self.addCleanup(self.dynamodb_patch.stop)

    def test_delete_removes_object(self):
        form = {'action': ['delete'], 'spec': ['17C'], 'key': ['specs/17C_label.png']}
        response = dashboard.handle_files_post('token', form)

        self.assertEqual(303, response['statusCode'])
        self.s3.delete_object.assert_called_once_with(Bucket=dashboard.SPEC_BUCKET_NAME, Key='specs/17C_label.png')

    def test_upload_replace_decodes_base64_and_sets_content_type(self):
        content = base64.b64encode(b'\x89PNGfakebytes').decode('utf-8')
        form = {
            'action': ['upload'],
            'spec': ['17C'],
            'key': ['specs/17C_label.png'],
            'content_b64': [content],
        }
        response = dashboard.handle_files_post('token', form)

        self.assertEqual(303, response['statusCode'])
        self.s3.put_object.assert_called_once_with(
            Bucket=dashboard.SPEC_BUCKET_NAME, Key='specs/17C_label.png',
            Body=b'\x89PNGfakebytes', ContentType='image/png',
        )

    def test_upload_new_builds_key_from_filename(self):
        content = base64.b64encode(b'{}').decode('utf-8')
        form = {
            'action': ['upload_new'],
            'spec': ['17D'],
            'filename': ['17D_config.json'],
            'content_b64': [content],
        }
        response = dashboard.handle_files_post('token', form)

        self.assertEqual(303, response['statusCode'])
        self.s3.put_object.assert_called_once_with(
            Bucket=dashboard.SPEC_BUCKET_NAME, Key='specs/17D_config.json',
            Body=b'{}', ContentType='application/json',
        )

    def test_upload_new_with_blank_filename_shows_error_instead_of_silent_noop(self):
        content = base64.b64encode(b'\x89PNGfakebytes').decode('utf-8')
        self.s3.list_objects_v2.return_value = {'Contents': []}
        form = {
            'action': ['upload_new'],
            'spec': ['14C'],
            'filename': [''],
            'content_b64': [content],
        }
        response = dashboard.handle_files_post('token', form)

        self.s3.put_object.assert_not_called()
        self.assertEqual(200, response['statusCode'])
        self.assertIn('no filename', response['body'])

    def test_save_json_valid_writes_object(self):
        form = {
            'action': ['save_json'],
            'spec': ['17C'],
            'key': ['specs/17C_config.json'],
            'content': ['{"a": 1}'],
        }
        response = dashboard.handle_files_post('token', form)

        self.assertEqual(303, response['statusCode'])
        self.s3.put_object.assert_called_once_with(
            Bucket=dashboard.SPEC_BUCKET_NAME, Key='specs/17C_config.json',
            Body=b'{"a": 1}', ContentType='application/json',
        )

    def test_save_json_invalid_does_not_write_and_returns_editor_with_error(self):
        self.s3.get_paginator = None  # list_spec_files uses list_objects_v2 directly
        self.s3.list_objects_v2.return_value = {'Contents': []}
        form = {
            'action': ['save_json'],
            'spec': ['17C'],
            'key': ['specs/17C_config.json'],
            'content': ['{not valid json'],
        }
        response = dashboard.handle_files_post('token', form)

        self.s3.put_object.assert_not_called()
        self.assertEqual(200, response['statusCode'])
        self.assertIn('Invalid JSON', response['body'])


class AuditLogJsonTests(unittest.TestCase):
    """format_='json' is consumed by DoleFrontEnd's Label Verification
    section -- same handler, same DynamoDB query, just serialized instead of
    rendered into the HTML dashboard."""

    def setUp(self):
        self.table = Mock()
        self.dynamodb = Mock()
        self.dynamodb.Table.return_value = self.table
        self.dynamodb_patch = patch.object(dashboard, 'dynamodb', self.dynamodb)
        self.dynamodb_patch.start()
        self.addCleanup(self.dynamodb_patch.stop)

    def test_json_format_returns_items_counts_and_next_key(self):
        self.table.query.return_value = {
            'Items': [
                {
                    'timestamp': '2026-09-17T05:37:59.356974+00:00',
                    'sender': '27833801959',
                    'sender_name': 'Evert',
                    'msg_type': 'image',
                    'spec_code': '15A',
                    'result': 'FAIL',
                    'detail': 'some detail',
                    'image_key': 'incoming/some.jpg',
                },
            ],
            'LastEvaluatedKey': {'record_type': 'REQUEST', 'timestamp': 'x'},
        }
        response = dashboard.handle_audit_log('token', {}, format_='json')

        self.assertEqual(200, response['statusCode'])
        self.assertEqual('application/json', response['headers']['Content-Type'])
        body = json.loads(response['body'])
        self.assertEqual(1, len(body['items']))
        self.assertEqual('15A', body['items'][0]['spec_code'])
        self.assertEqual({'FAIL': 1}, body['counts'])
        self.assertIsNotNone(body['next_key'])

    def test_json_format_omits_next_key_on_last_page(self):
        self.table.query.return_value = {'Items': []}
        response = dashboard.handle_audit_log('token', {}, format_='json')
        body = json.loads(response['body'])
        self.assertIsNone(body['next_key'])

    def test_html_format_unaffected(self):
        self.table.query.return_value = {'Items': []}
        response = dashboard.handle_audit_log('token', {}, format_=None)
        self.assertEqual('text/html; charset=utf-8', response['headers']['Content-Type'])


class ReferenceTablesJsonTests(unittest.TestCase):
    """format_='json' is consumed by DoleFrontEnd's Label Verification ->
    Reference Tables page -- same scan/put_item/delete_item logic as the
    HTML dashboard, just serialized instead of rendered."""

    def setUp(self):
        self.table = Mock()
        self.dynamodb = Mock()
        self.dynamodb.Table.return_value = self.table
        self.dynamodb_patch = patch.object(dashboard, 'dynamodb', self.dynamodb)
        self.dynamodb_patch.start()
        self.addCleanup(self.dynamodb_patch.stop)

    def test_get_json_returns_items_and_schema(self):
        self.table.scan.return_value = {'Items': [{'PUC': 'A1030', 'GGN': '4052852937450'}]}
        response = dashboard.handle_tables_get('token', {'table': 'whatsapp-puc'}, format_='json')

        self.assertEqual(200, response['statusCode'])
        self.assertEqual('application/json', response['headers']['Content-Type'])
        body = json.loads(response['body'])
        self.assertEqual('whatsapp-puc', body['table'])
        self.assertEqual(['PUC'], body['key_attrs'])
        self.assertIn('whatsapp-variety', body['available_tables'])
        self.assertEqual([{'PUC': 'A1030', 'GGN': '4052852937450'}], body['items'])

    def test_get_json_defaults_to_first_table_when_unknown(self):
        self.table.scan.return_value = {'Items': []}
        response = dashboard.handle_tables_get('token', {'table': 'not-a-real-table'}, format_='json')
        body = json.loads(response['body'])
        self.assertEqual(next(iter(dashboard.TABLE_REGISTRY)), body['table'])

    def test_save_json_writes_item_and_returns_success(self):
        form = {
            'table': ['whatsapp-puc'],
            'action': ['save'],
            'col::PUC': ['A1030'],
            'col::GGN': ['4052852937450'],
        }
        response = dashboard.handle_tables_post('token', form, format_='json')

        self.assertEqual(200, response['statusCode'])
        body = json.loads(response['body'])
        self.assertTrue(body['success'])
        self.table.put_item.assert_called_once_with(Item={'PUC': 'A1030', 'GGN': '4052852937450'})

    def test_save_json_missing_key_returns_400_and_does_not_write(self):
        form = {
            'table': ['whatsapp-variety'],
            'action': ['save'],
            'col::VarietyName': ['SUGRA35'],
            'col::Commodity': [''],
        }
        response = dashboard.handle_tables_post('token', form, format_='json')

        self.assertEqual(400, response['statusCode'])
        self.table.put_item.assert_not_called()

    def test_delete_json_builds_key_from_col_fields(self):
        form = {
            'table': ['whatsapp-puc'],
            'action': ['delete'],
            'col::PUC': ['A1030'],
        }
        response = dashboard.handle_tables_post('token', form, format_='json')

        self.assertEqual(200, response['statusCode'])
        body = json.loads(response['body'])
        self.assertTrue(body['success'])
        self.table.delete_item.assert_called_once_with(Key={'PUC': 'A1030'})

    def test_delete_json_missing_key_returns_400_and_does_not_delete(self):
        form = {
            'table': ['whatsapp-puc'],
            'action': ['delete'],
            'col::PUC': [''],
        }
        response = dashboard.handle_tables_post('token', form, format_='json')

        self.assertEqual(400, response['statusCode'])
        self.table.delete_item.assert_not_called()

    def test_html_format_unaffected(self):
        self.table.scan.return_value = {'Items': []}
        response = dashboard.handle_tables_get('token', {'table': 'whatsapp-puc'}, format_=None)
        self.assertEqual('text/html; charset=utf-8', response['headers']['Content-Type'])


class SpecFilesJsonTests(unittest.TestCase):
    """format_='json' is consumed by DoleFrontEnd's Label Verification ->
    Spec Files page -- same S3 list/get/put/delete calls as the HTML
    dashboard, just serialized instead of rendered."""

    def setUp(self):
        self.s3 = Mock()
        self.s3_patch = patch.object(dashboard, 's3_client', self.s3)
        self.s3_patch.start()
        self.addCleanup(self.s3_patch.stop)

        self.dynamodb = Mock()
        self.dynamodb.Table.return_value.scan.return_value = {'Items': []}
        self.dynamodb_patch = patch.object(dashboard, 'dynamodb', self.dynamodb)
        self.dynamodb_patch.start()
        self.addCleanup(self.dynamodb_patch.stop)

    def test_get_json_returns_groups_with_preview_urls(self):
        self.s3.list_objects_v2.return_value = {
            'Contents': [
                {'Key': 'specs/17C_label.png', 'Size': 2048, 'LastModified': 'x'},
                {'Key': 'specs/17C_config.json', 'Size': 512, 'LastModified': 'x'},
            ],
        }
        response = dashboard.handle_files_get('token', {}, format_='json')

        self.assertEqual(200, response['statusCode'])
        self.assertEqual('application/json', response['headers']['Content-Type'])
        body = json.loads(response['body'])
        files = body['groups']['17C']
        png = next(f for f in files if f['filename'] == '17C_label.png')
        cfg = next(f for f in files if f['filename'] == '17C_config.json')
        self.assertIsNotNone(png['preview_url'])
        self.assertFalse(png['is_json'])
        self.assertIsNone(cfg['preview_url'])
        self.assertTrue(cfg['is_json'])

    def test_get_json_content_key_returns_file_contents(self):
        self.s3.get_object.return_value = {'Body': Mock(read=lambda: b'{"a": 1}')}
        response = dashboard.handle_files_get('token', {'content_key': 'specs/17C_config.json'}, format_='json')

        body = json.loads(response['body'])
        self.assertEqual('{"a": 1}', body['content'])

    def test_get_json_content_key_missing_returns_empty_string(self):
        class FakeNoSuchKey(Exception):
            pass
        self.s3.exceptions.NoSuchKey = FakeNoSuchKey
        self.s3.get_object.side_effect = FakeNoSuchKey()
        response = dashboard.handle_files_get('token', {'content_key': 'specs/missing.json'}, format_='json')

        body = json.loads(response['body'])
        self.assertEqual('', body['content'])

    def test_delete_json_returns_success(self):
        form = {'action': ['delete'], 'spec': ['17C'], 'key': ['specs/17C_label.png']}
        response = dashboard.handle_files_post('token', form, format_='json')

        self.assertEqual(200, response['statusCode'])
        self.assertTrue(json.loads(response['body'])['success'])
        self.s3.delete_object.assert_called_once_with(Bucket=dashboard.SPEC_BUCKET_NAME, Key='specs/17C_label.png')

    def test_upload_json_missing_file_returns_400(self):
        form = {'action': ['upload_new'], 'spec': ['14C'], 'filename': ['14C_label.png'], 'content_b64': ['']}
        response = dashboard.handle_files_post('token', form, format_='json')

        self.assertEqual(400, response['statusCode'])
        self.s3.put_object.assert_not_called()

    def test_save_json_valid_returns_success(self):
        form = {'action': ['save_json'], 'spec': ['17C'], 'key': ['specs/17C_config.json'], 'content': ['{"a": 1}']}
        response = dashboard.handle_files_post('token', form, format_='json')

        self.assertEqual(200, response['statusCode'])
        self.assertTrue(json.loads(response['body'])['success'])

    def test_save_json_invalid_returns_400_and_does_not_write(self):
        form = {'action': ['save_json'], 'spec': ['17C'], 'key': ['specs/17C_config.json'], 'content': ['{not valid']}
        response = dashboard.handle_files_post('token', form, format_='json')

        self.assertEqual(400, response['statusCode'])
        self.assertIn('Invalid JSON', json.loads(response['body'])['error'])
        self.s3.put_object.assert_not_called()

    def test_html_format_unaffected(self):
        self.s3.list_objects_v2.return_value = {'Contents': []}
        response = dashboard.handle_files_get('token', {}, format_=None)
        self.assertEqual('text/html; charset=utf-8', response['headers']['Content-Type'])


if __name__ == '__main__':
    unittest.main()
