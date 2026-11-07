"""Updater transaction tests. No real git, systemd or external network."""
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]


class UpdateTransactionTests(unittest.TestCase):
    def test_retention_failure_does_not_roll_back_healthy_release(self):
        spec = importlib.util.spec_from_file_location('update_transaction', REPO / 'scripts/update_worker.py')
        worker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(worker)
        sha = 'a' * 40
        with tempfile.TemporaryDirectory() as tmp:
            worker.TARGET = Path(tmp)
            worker.REVISION = worker.TARGET / '.release-commit'
            worker.WORKER = worker.TARGET / 'worker.py'
            # Git and deployment are stubbed; the transaction sequence remains real.
            with (patch.object(worker, 'run_command', return_value=sha),
                  patch.object(worker, 'installed_sha', return_value='b' * 40),
                  patch.object(worker, 'safe_source'),
                  patch.object(worker, 'verify_existing_files'),
                  patch.object(worker, 'store_backup', return_value=Path(tmp)),
                  patch.object(worker, 'deploy_files'),
                  patch.object(worker, 'health', return_value=True),
                  patch.object(worker, 'write_manifest'),
                  patch.object(worker.shutil, 'copy2'),
                  patch.object(worker.os, 'chmod'),
                  patch.object(worker, 'prune_backups', side_effect=PermissionError('retention denied')),
                  patch.object(worker, 'rollback') as rollback,
                  patch.object(worker, 'write_status') as status):
                worker.perform(sha)
            rollback.assert_not_called()
            self.assertEqual(worker.REVISION.read_text().strip(), sha)
            self.assertEqual(status.call_args.args[:2], ('success', 'complete'))


if __name__ == '__main__':
    unittest.main()
