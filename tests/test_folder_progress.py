import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from copita.core.filehoster_downloader import FileHosterDownloader

class FolderProgressTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_child_speed_and_bytes_are_aggregated(self):
        events = []
        class FakeChunk:
            def __init__(self, **kwargs):
                self.options = kwargs
            async def start(self):
                self.options['progress_callback']({'percent':50, 'speed':1024, 'downloaded':5, 'total':10})
                await asyncio.sleep(0)
                Path(self.options['output_path']).write_bytes(b'1234567890')
        with tempfile.TemporaryDirectory() as directory:
            downloader = FileHosterDownloader('https://drive.google.com/folder', directory, progress_callback=events.append)
            with patch('copita.core.filehoster_downloader.ChunkDownloader', FakeChunk):
                await downloader._download_folder({'folder_name':'test', 'children':[{'name':'a.mp4','url':'a'},{'name':'b.mp4','url':'b'}]})
        self.assertTrue(any(e['speed'] == 2048 and e['downloaded'] == 10 for e in events))
        self.assertEqual(events[-1]['downloaded'], 20)
        self.assertEqual(events[-1]['total'], 20)
        self.assertEqual(events[-1]['speed'], 0)
        self.assertEqual(events[-1]['percent'], 100)

    async def test_child_failure_is_not_marked_complete(self):
        class BrokenChunk:
            def __init__(self, **kwargs): pass
            async def start(self): raise RuntimeError('HTTP 404')
        with tempfile.TemporaryDirectory() as directory:
            downloader = FileHosterDownloader('https://drive.google.com/folder', directory)
            with patch('copita.core.filehoster_downloader.ChunkDownloader', BrokenChunk):
                with self.assertRaisesRegex(ValueError, '1 of 1 files could not be downloaded'):
                    await downloader._download_folder({'folder_name':'test', 'children':[{'name':'a.mp4','url':'a'}]})

if __name__ == '__main__': unittest.main()