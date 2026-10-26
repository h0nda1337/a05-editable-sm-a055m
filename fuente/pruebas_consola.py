"""Display backpressure regressions; no process or USB access."""
import unittest
from a05_console import UiEventBuffer


class ConsoleTests(unittest.TestCase):
    def test_output_flood_is_bounded_and_latest_tail_survives(self):
        buffer = UiEventBuffer(max_output_chars=8192)
        for _ in range(10000):
            buffer.put(('stdout', 'x'*1000))
        buffer.put(('stdout', 'LATEST_OUTPUT_TAIL'))
        state = buffer.snapshot()
        self.assertLessEqual(state['peak_output_chars'], 8192)
        self.assertGreater(state['display_chars_omitted'], 9_000_000)
        displayed = []
        for _ in range(10):
            displayed.extend(buffer.drain(max_chars=2048))
        self.assertIn('LATEST_OUTPUT_TAIL', ''.join(text for kind, text in displayed if kind == 'stdout'))

    def test_latest_stage_and_completion_are_not_lost_in_flood(self):
        buffer = UiEventBuffer(max_output_chars=8192)
        for index in range(10000):
            buffer.put(('stderr', 'debug'*100))
            buffer.put(('stage', 'old stage '+str(index)))
        buffer.put(('stage', 'CURRENT_STAGE'))
        result = {'status': 'cancelled'}
        buffer.put(('finished', result))
        displayed = buffer.drain(max_chars=16384)
        self.assertIn(('stage', 'CURRENT_STAGE'), displayed)
        self.assertIn(('finished', result), displayed)
        self.assertEqual(len([1 for kind, value in displayed if kind == 'stage']), 1)

    def test_one_update_does_not_drain_entire_backlog(self):
        buffer = UiEventBuffer(max_output_chars=65536)
        for _ in range(40):
            buffer.put(('stdout', 'data'*1000))
        batch = buffer.drain(max_chars=2048, max_messages=4)
        self.assertLessEqual(sum(len(text) for kind, text in batch if kind == 'stdout'), 2048)
        self.assertGreater(buffer.snapshot()['pending_output_chars'], 0)


if __name__ == '__main__':
    unittest.main()
