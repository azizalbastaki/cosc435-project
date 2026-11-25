import unittest
from unittest.mock import patch
from pathlib import Path
from streamlit.testing.v1 import AppTest
from streamlit.proto.TextInput_pb2 import TextInput

ROOT=Path(__file__).resolve().parents[1]

class ScreenTests(unittest.TestCase):
    def test_all_screens_and_filter(self):
        # UI assertions must not depend on the user's growing live audit history.
        empty_history = patch('gui.backend.overview', return_value={
            'trader': [], 'guardian': [], 'reviews': [], 'samples': [],
            'sessions': [], 'decisions': {}, 'snapshot': {}})
        empty_history.start()
        self.addCleanup(empty_history.stop)
        app=AppTest.from_file(str(ROOT/'app.py')).run(timeout=30)
        self.assertTrue(any('TradeSentry' in x.value for x in app.markdown))
        for page in ['Overview','Live flow','Guardian','Activity','Setup']:
            app.sidebar.radio[0].set_value(page).run(timeout=30)
            self.assertFalse(app.exception, str(app.exception))
            if page=='Live flow':
                app.radio(key='flow_mode').set_value('Replay saved history').run()
                self.assertFalse(app.exception, str(app.exception))
            if page=='Guardian':
                app.selectbox[0].set_value('BLOCK').run()
                self.assertTrue(any('No decisions match' in x.value for x in app.info))
            if page=='Setup':
                self.assertTrue(any('at least every two weeks' in x.value for x in app.info))
                self.assertTrue(all(x.proto.type == TextInput.PASSWORD for x in app.text_input
                                    if x.label in ['Ledger trader key','Ledger Guardian key','Model provider key']))
        app.text_input[0].set_value('')
        next(button for button in app.button if button.label == 'Save workspace').click().run()
        self.assertTrue(any('Enter your name' in x.value for x in app.error))
        self.assertFalse(app.exception)

    def test_fetch_populates_form_without_saving_or_starting(self):
        fetched = dict(id=49, version=7, strategy='momentum', symbol='ETH',
                       params=dict(lookback_minutes=10, threshold_pct=0.5, position_fraction=0.05,
                                   stop_loss_pct=2, take_profit_pct=3),
                       validation_error='take_profit_pct outside the validated safe range')
        with patch('gui.backend.fetch_theory_settings', return_value=fetched) as fetch, \
                patch('gui.backend.save_setup') as save, patch('gui.backend.SessionController.start') as start:
            app = AppTest.from_file(str(ROOT/'app.py')).run(timeout=30)
            app.sidebar.radio[0].set_value('Setup').run()
            app.button(key='fetch_theory').click().run()
            self.assertFalse(app.exception, str(app.exception))
            fetch.assert_called_once()
            self.assertEqual(app.selectbox(key='setup_strategy').value, 'momentum')
            self.assertEqual(app.selectbox(key='setup_symbol').value, 'ETH')
            self.assertEqual(app.number_input(key='setup_version').value, 7)
            self.assertIn('"take_profit_pct": 3', app.text_area(key='setup_params').value)
            self.assertTrue(any('local trading validation still rejects' in x.value for x in app.warning))
            save.assert_not_called()
            start.assert_not_called()

if __name__=='__main__': unittest.main()
