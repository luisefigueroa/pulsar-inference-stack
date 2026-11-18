"""Compare what a command printed with what the integration contract says of it."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts import integration_contract


def assert_document_kind(test, operation, document):
    """DOCUMENT is what OPERATION printed under --json: its own JSON document,
    not the result envelope, with the kind the contract names, or without a
    kind when the contract names none."""
    output = integration_contract.contract()['operation_outputs'][operation]
    test.assertEqual(output['form'], 'document', operation)
    test.assertIsInstance(document, dict, operation)
    # The envelope has ok beside result or error; a document has its own keys.
    test.assertFalse('ok' in document and ('result' in document or 'error' in document), operation)
    if output['kind'] is None:
        test.assertNotIn('kind', document, operation)
    else:
        test.assertEqual(document.get('kind'), output['kind'], operation)
