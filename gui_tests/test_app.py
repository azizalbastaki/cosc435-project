import unittest
from pathlib import Path
from streamlit.testing.v1 import AppTest

ROOT=Path(__file__).resolve().parents[1]

class ScreenTests(unittest.TestCase):
    def test_all_screens_and_filter(self):
        app=AppTest.from_file(str(ROOT/'app.py')).run(timeout=30)
        for page in ['Overview','Live flow','Guardian','Activity','Setup']:
            app.sidebar.radio[0].set_value(page).run(timeout=30)
            self.assertFalse(app.exception, str(app.exception))
            if page=='Live flow':
                app.radio(key='flow_mode').set_value('Replay saved history').run()
                self.assertFalse(app.exception, str(app.exception))
            if page=='Guardian':
                app.selectbox[0].set_value('BLOCK').run()
                self.assertTrue(any('No decisions match' in x.value for x in app.info))
        app.text_input[0].set_value('')
        app.button[0].click().run()
        self.assertTrue(any('Enter your name' in x.value for x in app.error))
        self.assertFalse(app.exception)

if __name__=='__main__': unittest.main()
