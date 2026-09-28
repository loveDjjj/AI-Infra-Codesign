"""验证课程轨迹导出覆盖独立会话，清理调用日志后仍能重建清单。"""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from codesign_lab.traces import export, register_sessions


class TraceExportChecks(unittest.TestCase):
    def test_session_index_feeds_export(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / 'state.sqlite'
            connection = sqlite3.connect(database)
            connection.executescript('''
                CREATE TABLE threads(id TEXT, rollout_path TEXT, agent_path TEXT,
                    created_at_ms INTEGER);
                CREATE TABLE thread_spawn_edges(parent_thread_id TEXT, child_thread_id TEXT);
            ''')
            for name in ('root-session', 'planner-session'):
                source = root / (name + '.jsonl')
                source.write_text(json.dumps({'session': name}) + '\n')
                connection.execute('INSERT INTO threads VALUES(?,?,?,0)',
                                   (name, str(source), name))
            connection.commit()
            connection.close()
            destination = root / 'export'
            register_sessions(destination, json.dumps({'type': 'thread.started',
                'thread_id': 'planner-session'}), role='planner', key='global/one')
            result = export(destination, database, 'root-session')
            self.assertEqual(len(result['sessions']), 2)

    def test_independent_session_survives_log_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / 'state.sqlite'
            connection = sqlite3.connect(database)
            connection.executescript('''
                CREATE TABLE threads(id TEXT, rollout_path TEXT, agent_path TEXT,
                    created_at_ms INTEGER);
                CREATE TABLE thread_spawn_edges(parent_thread_id TEXT, child_thread_id TEXT);
            ''')
            for index, name in enumerate(('root-session', 'independent-session')):
                path = root / (name + '.jsonl')
                path.write_text(json.dumps({'session': name}) + '\n')
                connection.execute('INSERT INTO threads VALUES(?,?,?,?)',
                                   (name, str(path), name, index))
            connection.commit()
            connection.close()
            destination = root / 'export'
            events = destination / 'pipeline/global/call.events.jsonl'
            events.parent.mkdir(parents=True)
            events.write_text(json.dumps({'type': 'thread.started',
                                          'thread_id': 'independent-session'}) + '\n')
            first = export(destination, database, 'root-session')
            self.assertEqual(len(first['sessions']), 2)
            events.unlink()
            second = export(destination, database, 'root-session')
            self.assertEqual({entry['thread_id'] for entry in second['sessions']},
                             {'root-session', 'independent-session'})


if __name__ == '__main__':
    unittest.main()
