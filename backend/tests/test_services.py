import unittest
from unittest.mock import patch, MagicMock
from services.ai_vision import analyze_vehicle_photo
from services.document_processing import process_document
from services.payment_processing import process_payment
from integrations.stripe_api import create_payment_intent

class TestAIVisionService(unittest.TestCase):
    @patch('services.ai_vision.vision_client')
    def test_analyze_vehicle_photo_success(self, mock_vision_client):
        mock_vision_client.detect_labels.return_value = [
            {'Name': 'Car', 'Confidence': 99.9},
            {'Name': 'Sedan', 'Confidence': 95.5},
            {'Name': 'Vehicle', 'Confidence': 99.8}
        ]
        result = analyze_vehicle_photo('test_photo.jpg')
        self.assertIn('Car', result)
        self.assertIn('Sedan', result)
        self.assertIn('Vehicle', result)

    @patch('services.ai_vision.vision_client')
    def test_analyze_vehicle_photo_no_vehicle(self, mock_vision_client):
        mock_vision_client.detect_labels.return_value = [
            {'Name': 'Person', 'Confidence': 99.9},
            {'Name': 'Outdoors', 'Confidence': 95.5}
        ]
        with self.assertRaises(ValueError):
            analyze_vehicle_photo('test_photo.jpg')

class TestDocumentProcessingService(unittest.TestCase):
    @patch('services.document_processing.textract_client')
    def test_process_document_success(self, mock_textract_client):
        mock_textract_client.detect_document_text.return_value = {
            'Blocks': [
                {'BlockType': 'LINE', 'Text': 'Vehicle Registration'},
                {'BlockType': 'LINE', 'Text': 'License Plate: ABC123'}
            ]
        }
        result = process_document('test_document.pdf')
        self.assertIn('Vehicle Registration', result)
        self.assertIn('License Plate: ABC123', result)

    @patch('services.document_processing.textract_client')
    def test_process_document_empty(self, mock_textract_client):
        mock_textract_client.detect_document_text.return_value = {'Blocks': []}
        with self.assertRaises(ValueError):
            process_document('test_document.pdf')

class TestPaymentProcessingService(unittest.TestCase):
    @patch('services.payment_processing.stripe')
    def test_process_payment_success(self, mock_stripe):
        mock_stripe.PaymentIntent.create.return_value = {'id': 'pi_123456', 'status': 'succeeded'}
        result = process_payment(1000, 'usd', 'card_token_123')
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(result['id'], 'pi_123456')

    @patch('services.payment_processing.stripe')
    def test_process_payment_failure(self, mock_stripe):
        mock_stripe.PaymentIntent.create.side_effect = Exception('Payment failed')
        with self.assertRaises(Exception):
            process_payment(1000, 'usd', 'card_token_123')

class TestStripeIntegration(unittest.TestCase):
    @patch('integrations.stripe_api.stripe')
    def test_create_payment_intent_success(self, mock_stripe):
        mock_stripe.PaymentIntent.create.return_value = {'id': 'pi_123456', 'client_secret': 'secret_123'}
        result = create_payment_intent(2000, 'usd')
        self.assertEqual(result['id'], 'pi_123456')
        self.assertEqual(result['client_secret'], 'secret_123')

    @patch('integrations.stripe_api.stripe')
    def test_create_payment_intent_failure(self, mock_stripe):
        mock_stripe.PaymentIntent.create.side_effect = Exception('Invalid currency')
        with self.assertRaises(Exception):
            create_payment_intent(2000, 'invalid_currency')

if __name__ == '__main__':
    unittest.main()