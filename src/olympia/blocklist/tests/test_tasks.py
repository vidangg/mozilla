import json
import os
from datetime import datetime, timedelta
from unittest import TestCase, mock

from django.conf import settings
from django.contrib.admin.models import LogEntry
from django.core.exceptions import SuspiciousOperation
from django.test.testcases import TransactionTestCase

import pytest

from olympia import amo
from olympia.amo.tests import (
    addon_factory,
    block_factory,
    user_factory,
    version_factory,
)
from olympia.blocklist.mlbf import MLBF
from olympia.constants.blocklist import (
    BlockListAction,
)
from olympia.zadmin.models import get_config, set_config

from ..models import BlocklistSubmission, BlockType, BlockVersion
from ..tasks import (
    BLOCKLIST_RECORD_MLBF_BASE,
    cleanup_old_files,
    process_blocklistsubmission,
    upload_filter,
)
from ..utils import datetime_to_ts, get_mlbf_base_id_config_key


def test_cleanup_old_files():
    mlbf_path = settings.MLBF_STORAGE_PATH
    six_month_date = datetime.now() - timedelta(weeks=26)

    now_dir = os.path.join(mlbf_path, str(datetime_to_ts()))
    os.mkdir(now_dir)

    over_six_month_dir = os.path.join(
        mlbf_path, str(datetime_to_ts(six_month_date - timedelta(days=1)))
    )
    os.mkdir(over_six_month_dir)
    with open(os.path.join(over_six_month_dir, 'f'), 'w') as over:
        over.write('.')  # create a file that'll be deleted too

    under_six_month_dir = os.path.join(
        mlbf_path, str(datetime_to_ts(six_month_date - timedelta(days=-1)))
    )
    os.mkdir(under_six_month_dir)

    cleanup_old_files(base_filter_id=9600100364318)  # sometime in the future
    assert os.path.exists(now_dir)
    assert os.path.exists(under_six_month_dir)
    assert not os.path.exists(over_six_month_dir)

    # repeat, but with a base filter id that's over 6 months
    os.mkdir(over_six_month_dir)  # recreate it

    after_base_date_dir = os.path.join(
        mlbf_path, str(datetime_to_ts(six_month_date - timedelta(weeks=1, days=1)))
    )
    os.mkdir(after_base_date_dir)
    with open(os.path.join(after_base_date_dir, 'f'), 'w') as over:
        over.write('.')  # create a file that'll be deleted too

    cleanup_old_files(
        base_filter_id=datetime_to_ts(six_month_date - timedelta(weeks=1))
    )
    assert os.path.exists(now_dir)
    assert os.path.exists(under_six_month_dir)
    assert os.path.exists(over_six_month_dir)
    assert not os.path.exists(after_base_date_dir)


class TestProcessBlocklistSubmission(TransactionTestCase):
    def setUp(self):
        user_factory(id=settings.TASK_USER_ID)
        self.addon = addon_factory(guid='guid@')
        self.submission = BlocklistSubmission.objects.create(
            input_guids=self.addon.guid,
            signoff_state=BlocklistSubmission.SIGNOFF_STATES.APPROVED,
        )

    def test_state_reset(self):
        with mock.patch.object(
            BlocklistSubmission,
            'save_to_block_objects',
            side_effect=SuspiciousOperation('Something happened!'),
        ):
            with self.assertRaises(SuspiciousOperation):
                # we know it's going to raise, we just want to capture it safely
                process_blocklistsubmission.delay(self.submission.id)
        self.submission.reload()
        assert (
            self.submission.signoff_state == BlocklistSubmission.SIGNOFF_STATES.PENDING
        )
        log_entry = LogEntry.objects.get()
        assert log_entry.user.id == settings.TASK_USER_ID
        assert log_entry.change_message == 'Exception in task: Something happened!'

    @mock.patch.object(BlocklistSubmission, 'save_to_block_objects')
    @mock.patch.object(BlocklistSubmission, 'delete_block_objects')
    def test_calls_save_to_block_objects_or_delete_block_objects_depending_on_action(
        self, delete_block_objects_mock, save_to_block_objects_mock
    ):
        for action in (
            BlocklistSubmission.ACTIONS.ADDCHANGE,
            BlocklistSubmission.ACTIONS.HARDEN,
            BlocklistSubmission.ACTIONS.SOFTEN,
        ):
            save_to_block_objects_mock.reset_mock()
            delete_block_objects_mock.reset_mock()
            self.submission.update(action=action)
            process_blocklistsubmission(self.submission.id)
            assert save_to_block_objects_mock.call_count == 1
            assert delete_block_objects_mock.call_count == 0
        for action in (BlocklistSubmission.ACTIONS.DELETE,):
            save_to_block_objects_mock.reset_mock()
            delete_block_objects_mock.reset_mock()
            self.submission.update(action=action)
            process_blocklistsubmission(self.submission.id)
            assert save_to_block_objects_mock.call_count == 0
            assert delete_block_objects_mock.call_count == 1


@pytest.mark.django_db
class TestUploadMLBFToRemoteSettings(TestCase):
    def _attachment(self, id, attachment_type, generation_time):
        return {
            'id': id,
            'attachment': {},
            'generation_time': generation_time,
            'attachment_type': attachment_type,
        }

    def _stash(self, id, stash_time):
        return {'id': id, 'stash': {}, 'stash_time': stash_time}

    def setUp(self):
        self.user = user_factory()
        self.addon = addon_factory()
        self.block = block_factory(guid=self.addon.guid, updated_by=self.user)

        prefix = 'olympia.blocklist.tasks.'
        self.mocks = {
            'records': f'{prefix}RemoteSettings.records',
            'delete_record': f'{prefix}RemoteSettings.delete_record',
            'publish_attachment': f'{prefix}RemoteSettings.publish_attachment',
            'publish_record': f'{prefix}RemoteSettings.publish_record',
            'complete_session': f'{prefix}RemoteSettings.complete_session',
            'set_config': f'{prefix}set_config',
            'statsd.incr': f'{prefix}statsd.incr',
            'cleanup_old_files.delay': f'{prefix}cleanup_old_files.delay',
        }
        for mock_name, mock_path in self.mocks.items():
            patcher = mock.patch(mock_path)
            self.addCleanup(patcher.stop)
            self.mocks[mock_name] = patcher.start()

        self.generation_time = datetime_to_ts(datetime.now())

    def _block_version(
        self, block=None, version=None, block_type=BlockType.BLOCKED, is_signed=True
    ):
        block = block or self.block
        version = version or version_factory(
            addon=self.addon, file_kw={'is_signed': is_signed}
        )
        return BlockVersion.objects.create(
            block=block, version=version, block_type=block_type
        )

    def test_invalid_action_raises(self):
        with self.assertRaises(ValueError):
            BLOCKLIST_RECORD_MLBF_BASE('foo')

        MLBF.generate_from_db(self.generation_time)

        with self.assertRaises(KeyError):
            upload_filter(
                self.generation_time,
                actions=['foo'],
            )

    def _test_upload_base_filter(self, *block_types: BlockType):
        self._block_version(is_signed=True)
        mlbf = MLBF.generate_from_db(self.generation_time)
        for block_type in block_types:
            mlbf.generate_and_write_filter(block_type)

        actions_map = {
            BlockType.BLOCKED: BlockListAction.UPLOAD_BLOCKED_FILTER.name,
            BlockType.SOFT_BLOCKED: BlockListAction.UPLOAD_SOFT_BLOCKED_FILTER.name,
        }

        upload_filter(
            self.generation_time,
            actions=[actions_map[block_type] for block_type in block_types],
        )

        assert not self.mocks['delete_record'].called
        actual_files = [
            mock[0][1][1].name
            for mock in self.mocks['publish_attachment'].call_args_list
        ]

        for block_type in block_types:
            with mlbf.storage.open(mlbf.filter_path(block_type), 'rb') as filter_file:
                assert filter_file.name in actual_files
                expected_call = mock.call(
                    {
                        'key_format': MLBF.KEY_FORMAT,
                        'generation_time': self.generation_time,
                        'attachment_type': BLOCKLIST_RECORD_MLBF_BASE(block_type),
                    },
                    ('filter.bin', mock.ANY, 'application/octet-stream'),
                )
                assert expected_call in self.mocks['publish_attachment'].call_args_list

        assert self.mocks['complete_session'].called
        assert (
            mock.call(amo.config_keys.BLOCKLIST_MLBF_TIME, self.generation_time)
        ) in self.mocks['set_config'].call_args_list

        for block_type in block_types:
            if block_type == BlockType.BLOCKED:
                # We currently write to the old singular config key for hard blocks
                # to preserve backward compatibility.
                # In https://github.com/mozilla/addons/issues/15193
                # we can remove this and start writing to the new plural key.
                assert (
                    mock.call(
                        get_mlbf_base_id_config_key(block_type, compat=True),
                        self.generation_time,
                    )
                ) in self.mocks['set_config'].call_args_list
            assert (
                mock.call(get_mlbf_base_id_config_key(block_type), self.generation_time)
            ) in self.mocks['set_config'].call_args_list

    def test_upload_blocked_filter(self):
        self._test_upload_base_filter(BlockType.BLOCKED)

    def test_upload_soft_blocked_filter(self):
        self._test_upload_base_filter(BlockType.SOFT_BLOCKED)

    def test_upload_soft_and_blocked_filter(self):
        self._test_upload_base_filter(BlockType.BLOCKED, BlockType.SOFT_BLOCKED)

    def test_cleanup_old_files_called_with_oldest_config_key(self):
        """
        We clean up stale records, older than the oldest config key,
        even if no actions are provided. This ensures that we do not
        let stale data persist in the remote settings collection.
        """
        MLBF.generate_from_db(self.generation_time)
        self.mocks['records'].return_value = [
            self._attachment(0, 'bloomfilter-base', self.generation_time),
            self._stash(1, self.generation_time - 1),
        ]

        upload_filter(self.generation_time, actions=[])

        assert (
            get_config(get_mlbf_base_id_config_key(BlockType.BLOCKED, compat=True))
            is None
        )
        # If there is no base filter id, then we pass none and let the task
        # figure out what to do with that information.
        assert (
            mock.call(base_filter_id=None)
            in self.mocks['cleanup_old_files.delay'].call_args_list
        )

        set_config(
            get_mlbf_base_id_config_key(BlockType.BLOCKED, compat=True),
            self.generation_time,
        )
        upload_filter(self.generation_time, actions=[])

        # We should now pass the oldest config key to the cleanup task
        assert (
            mock.call(base_filter_id=self.generation_time)
            in self.mocks['cleanup_old_files.delay'].call_args_list
        )

    def test_delete_matching_attachment_when_uploading_filters(self):
        """
        When we upload a new filter, given the filter type is enabled, we should
        delete any existing attachment with the same name.
        """
        mlbf = MLBF.generate_from_db(self.generation_time)
        mlbf.generate_and_write_filter(BlockType.BLOCKED)
        mlbf.generate_and_write_filter(BlockType.SOFT_BLOCKED)

        self.mocks['records'].return_value = [
            self._attachment(0, 'bloomfilter-base', self.generation_time - 1),
            self._attachment(1, 'softblocks-bloomfilter-base', self.generation_time),
        ]

        upload_filter(
            self.generation_time, actions=[BlockListAction.UPLOAD_BLOCKED_FILTER.name]
        )

        # The previously uploaded blocked filter record "0" should be deleted
        assert self.mocks['delete_record'].call_args_list == [mock.call(0)]

        upload_filter(
            self.generation_time,
            actions=[BlockListAction.UPLOAD_SOFT_BLOCKED_FILTER.name],
        )
        assert mock.call(1) in self.mocks['delete_record'].call_args_list

    def test_clear_stash_deletes_all_stash_records(self):
        """
        When CLEAR_STASH action is provided, we should delete all stash records.
        """
        t0 = self.generation_time - 2
        t1 = self.generation_time - 1
        t2 = self.generation_time
        mlbf = MLBF.generate_from_db(t2)
        mlbf.generate_and_write_filter(BlockType.BLOCKED)
        mlbf.generate_and_write_stash()

        self.mocks['records'].return_value = [
            self._attachment(0, 'bloomfilter-base', t0),
            self._stash(1, t1),
        ]

        upload_filter(t2, actions=[BlockListAction.CLEAR_STASH.name])

        # We should only delete the stash record, not the attachment
        assert self.mocks['delete_record'].call_args_list == [
            mock.call(1),
        ]

    def test_upload_stash_sends_correct_data(self):
        """
        When there is an UPLOAD_STASH action, we should upload the correct data,
        commit the data, and update the config key indicating a change was made.
        """
        old_mlbf = MLBF.generate_from_db(self.generation_time - 1)
        blocked_version = self._block_version(is_signed=True)
        mlbf = MLBF.generate_from_db(self.generation_time)
        mlbf.generate_and_write_stash(old_mlbf)

        upload_filter(self.generation_time, actions=[BlockListAction.UPLOAD_STASH.name])

        with mlbf.storage.open(mlbf.stash_path, 'rb') as stash_file:
            actual_stash = self.mocks['publish_record'].call_args_list[0][0][0]
            stash_data = json.load(stash_file)

            assert actual_stash == {
                'key_format': MLBF.KEY_FORMAT,
                'stash_time': self.generation_time,
                'stash': stash_data,
            }

            assert actual_stash['stash']['blocked'] == MLBF.hash_filter_inputs(
                [(blocked_version.block.guid, blocked_version.version.version)]
            )

        assert (
            mock.call('blocklist.tasks.upload_filter.upload_stash')
            in self.mocks['statsd.incr'].call_args_list
        )

        assert self.mocks['complete_session'].called
        assert (
            mock.call(amo.config_keys.BLOCKLIST_MLBF_TIME, self.generation_time)
            in self.mocks['set_config'].call_args_list
        )

    def test_upload_filter_raises_when_no_filter_exists(self):
        with self.assertRaises(FileNotFoundError):
            upload_filter(
                self.generation_time,
                actions=[BlockListAction.UPLOAD_BLOCKED_FILTER.name],
            )

    def test_upload_stash_raises_when_no_stash_exists(self):
        with self.assertRaises(FileNotFoundError):
            upload_filter(
                self.generation_time, actions=[BlockListAction.UPLOAD_STASH.name]
            )

    def test_no_passed_actions_is_no_op(self):
        """
        If no actions are provided we should do nothing.
        """
        MLBF.generate_from_db(self.generation_time)
        upload_filter(self.generation_time, actions=[])

        # We should query existing records, commit the session,
        # set the config to indicate the task was run, clean up old files,
        # and increment the statsd counter.
        assert self.mocks['records'].call_count == 1
        assert self.mocks['complete_session'].call_count == 1
        assert (
            mock.call(amo.config_keys.BLOCKLIST_MLBF_TIME, self.generation_time)
            in self.mocks['set_config'].call_args_list
        )
        assert (
            mock.call('blocklist.tasks.upload_filter.reset_collection')
            in self.mocks['statsd.incr'].call_args_list
        )
        # If there is no existing config key, we pass None to cleanup_old_files
        # also triggering a no-op on that task.
        assert (
            mock.call(base_filter_id=None)
            in self.mocks['cleanup_old_files.delay'].call_args_list
        )

        # We should not modify any records in RemoteSettings
        assert self.mocks['publish_attachment'].call_count == 0
        assert self.mocks['publish_record'].call_count == 0
        assert self.mocks['delete_record'].call_count == 0

    def test_complete_session_is_called_after_all_actions(self):
        """
        server.complete_session is called to "commit" all of the changes made
        to remote settings. For this reason, it is important that we call it
        after executing any actions.
        """
        mlbf = MLBF.generate_from_db(self.generation_time)
        mlbf.generate_and_write_filter(BlockType.BLOCKED)
        mlbf.generate_and_write_stash()

        mocks = [
            'delete_record',
            'complete_session',
            'publish_record',
            'publish_attachment',
        ]
        manager = mock.Mock()

        for mock_name in mocks:
            mock_patch = self.mocks[mock_name]
            manager.attach_mock(mock_patch, mock_name)

        attachment_record = self._attachment(
            0, 'bloomfilter-base', self.generation_time - 1
        )
        stash_record = self._stash(1, self.generation_time - 2)

        manager.records.return_value = [attachment_record, stash_record]

        with mock.patch('olympia.blocklist.tasks.RemoteSettings', return_value=manager):
            upload_filter(
                self.generation_time,
                actions=[
                    BlockListAction.UPLOAD_BLOCKED_FILTER.name,
                    BlockListAction.UPLOAD_STASH.name,
                    BlockListAction.CLEAR_STASH.name,
                ],
            )

        manager.assert_has_calls(
            [
                # First we download the old records
                mock.call.records(),
                # Then we publish the new attachment
                mock.call.publish_attachment(mock.ANY, mock.ANY),
                # Then we publish the new stash
                mock.call.publish_record(mock.ANY),
                # Then we delete the old attachment
                mock.call.delete_record(0),
                # Then we delete the old stash
                mock.call.delete_record(1),
                # Then we commit the changes
                mock.call.complete_session(),
            ]
        )

    def test_config_is_updated_only_after_complete_session(self):
        """
        We should not update the config until after we've committed the session
        Since this call to complete session will raise an exception, the config
        should not be updated.
        """
        mlbf = MLBF.generate_from_db(self.generation_time)
        mlbf.generate_and_write_filter(BlockType.BLOCKED)

        self.mocks['complete_session'].side_effect = Exception('Something went wrong')

        with self.assertRaises(Exception):  # noqa: B017
            upload_filter(
                self.generation_time,
                actions=[
                    BlockListAction.UPLOAD_BLOCKED_FILTER.name,
                ],
            )

        assert self.mocks['complete_session'].called
        assert not self.mocks['set_config'].called
