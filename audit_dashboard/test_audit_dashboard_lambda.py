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


if __name__ == '__main__':
    unittest.main()
