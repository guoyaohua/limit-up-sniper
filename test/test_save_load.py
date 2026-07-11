import unittest
import os
import shutil
from multiprocessing import Manager
from datetime import datetime

# Mock logger to avoid dependency on the full logging setup
class MockLogger:
    def info(self, msg):
        print(f"INFO: {msg}")
    def error(self, msg):
        print(f"ERROR: {msg}")
    def warning(self, msg):
        print(f"WARNING: {msg}")
    def debug(self, msg):
        print(f"DEBUG: {msg}")
    def exception(self, msg):
        print(f"EXCEPTION: {msg}")

logger = MockLogger()

# Import the functions from their post-migration module.
from config import STRATEGY_NAME
from data.serialization import (
    save_shared_data,
    load_shared_data,
    deep_serialize,
    deep_restore
)

class TestSaveLoadSharedData(unittest.TestCase):

    def setUp(self):
        """Set up a temporary directory for test files."""
        self.test_dir = "temp_test_data"
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir)
        os.makedirs(self.test_dir)

    def tearDown(self):
        """Clean up the temporary directory."""
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_save_and_load(self):
        """Test saving and loading of shared_data."""
        with Manager() as manager:
            # 1. Create complex sample data
            original_shared_data = manager.dict({
                'str_key': 'hello_world',
                'int_key': 12345,
                'float_key': 3.14159,
                'bool_key': True,
                'list_key': manager.list([1, 'a', 3.0]),
                'dict_key': manager.dict({
                    'nested_str': 'nested_value',
                    'nested_list': manager.list([{'a': 1}, {'b': 2}])
                }),
                'value_key': manager.Value('d', 123.456)
            })

            # 2. Save the data
            save_result = save_shared_data(original_shared_data, self.test_dir)
            self.assertTrue(save_result, "save_shared_data should return True on success")

            # Verify that the file was created with the correct date stamp
            today_str = datetime.now().strftime('%Y%m%d')
            expected_file = os.path.join(
                self.test_dir, STRATEGY_NAME,
                f"shared_data_backup_{today_str}.pkl"
            )
            self.assertTrue(os.path.exists(expected_file), f"Data file '{expected_file}' should exist after saving")

            # 3. Load the data
            loaded_shared_data = load_shared_data(self.test_dir)
            self.assertIsNotNone(loaded_shared_data, "Loaded data should not be None")

            # 4. Compare the data
            # Convert both to regular dicts for easy comparison
            original_dict = deep_serialize(original_shared_data)
            loaded_dict = deep_serialize(loaded_shared_data)

            # Restored shared_data intentionally uses a plain top-level dict so
            # it can contain native multiprocessing.Value/Array on Windows.
            original_value = original_dict.get('_value_', original_dict)
            loaded_value = loaded_dict.get('_value_', loaded_dict)
            self.assertEqual(
                original_value['value_key']['_typecode_'],
                loaded_value['value_key']['_typecode_'],
            )
            self.assertAlmostEqual(
                original_value['value_key']['_value_'],
                loaded_value['value_key']['_value_'],
                places=4,
            )
            original_value.pop('value_key')
            loaded_value.pop('value_key')
            self.assertDictEqual(original_value, loaded_value)

if __name__ == '__main__':
    # To run this test, you might need to adjust sys.path if run from a different directory
    import sys
    sys.path.append(os.getcwd())
    
    # Since the original script has top-level code that connects to xtdata,
    # we cannot run it directly with `python -m unittest`.
    # We will run the test suite manually.
    suite = unittest.TestSuite()
    suite.addTest(unittest.makeSuite(TestSaveLoadSharedData))
    runner = unittest.TextTestRunner()
    print("Running save/load tests...")
    result = runner.run(suite)
    if result.wasSuccessful():
        print("All tests passed successfully!")
    else:
        print("Tests failed.")
