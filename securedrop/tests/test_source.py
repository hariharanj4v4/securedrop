# -*- coding: utf-8 -*-
import gzip
import json
import re

from cStringIO import StringIO
from flask import session, escape, url_for, current_app
from flask_testing import TestCase
from mock import patch, ANY

from sdconfig import config
import source
import utils
import version

from db import db
from models import Source
from source_app import main as source_app_main
from utils.db_helper import new_codename
from utils.instrument import InstrumentedApp

overly_long_codename = 'a' * (Source.MAX_CODENAME_LEN + 1)


class TestPytestSourceApp:

    def test_page_not_found(self, source_app):
        """Verify the page not found condition returns the intended template"""
        with InstrumentedApp(source_app) as ins:
            with source_app.test_client() as app:
                resp = app.get('UNKNOWN')
                assert resp.status_code == 404
                ins.assert_template_used('notfound.html')

    def test_index(self, source_app):
        """Test that the landing page loads and looks how we expect"""
        with source_app.test_client() as app:
            resp = app.get('/')
            assert resp.status_code == 200
            text = resp.data.decode('utf-8')
            assert 'Submit documents for the first time' in text
            assert 'Already submitted something?' in text

    def test_all_words_in_wordlist_validate(self, source_app):
        """Verify that all words in the wordlist are allowed by the form
        validation. Otherwise a source will have a codename and be unable to
        return."""

        with source_app.app_context():
            wordlist_en = current_app.crypto_util.get_wordlist('en')

        # chunk the words to cut down on the number of requets we make
        # otherwise this test is *slow*
        chunks = [wordlist_en[i:i + 7] for i in range(0, len(wordlist_en), 7)]

        with source_app.test_client() as app:
            for words in chunks:
                resp = app.post('/login', data=dict(codename=' '.join(words)),
                                follow_redirects=True)
                assert resp.status_code == 200
                text = resp.data.decode('utf-8')
                # If the word does not validate, then it will show
                # 'Invalid input'. If it does validate, it should show that
                # it isn't a recognized codename.
                assert 'Sorry, that is not a recognized codename.' in text
                assert 'logged_in' not in session

    def _find_codename(self, html):
        """Find a source codename (diceware passphrase) in HTML"""
        # Codenames may contain HTML escape characters, and the wordlist
        # contains various symbols.
        codename_re = (r'<p [^>]*id="codename"[^>]*>'
                       r'(?P<codename>[a-z0-9 &#;?:=@_.*+()\'"$%!-]+)</p>')
        codename_match = re.search(codename_re, html)
        assert codename_match is not None
        return codename_match.group('codename')

    def test_generate(self, source_app):
        with source_app.test_client() as app:
            resp = app.get('/generate')
            assert resp.status_code == 200
            session_codename = session['codename']

        text = resp.data.decode('utf-8')
        assert "This codename is what you will use in future visits" in text

        codename = self._find_codename(resp.data)
        assert len(codename.split()) == Source.NUM_WORDS
        # codename is also stored in the session - make sure it matches the
        # codename displayed to the source
        assert codename == escape(session_codename)

    def test_generate_already_logged_in(self, source_app):
        with source_app.test_client() as app:
            new_codename(app, session)
            # Make sure it redirects to /lookup when logged in
            resp = app.get('/generate')
            assert resp.status_code == 302
            # Make sure it flashes the message on the lookup page
            resp = app.get('/generate', follow_redirects=True)
            # Should redirect to /lookup
            assert resp.status_code == 200
            text = resp.data.decode('utf-8')
            assert "because you are already logged in." in text

    def test_create_new_source(self, source_app):
        with source_app.test_client() as app:
            resp = app.get('/generate')
            assert resp.status_code == 200
            resp = app.post('/create', follow_redirects=True)
            assert session['logged_in'] is True
            # should be redirected to /lookup
            text = resp.data.decode('utf-8')
            assert "Submit Materials" in text

    def test_generate_too_long_codename(self, source_app):
        """Generate a codename that exceeds the maximum codename length"""

        with patch.object(source_app.logger, 'warning') as logger:
            with patch.object(crypto_util.CryptoUtil, 'genrandomid',
                              side_effect=[overly_long_codename,
                                           'short codename']):
                with source_app.test_client() as app:
                    resp = app.post('/generate')
                    assert resp.status_code == 200

        logger.assert_called_with(
            "Generated a source codename that was too long, "
            "skipping it. This should not happen. "
            "(Codename='{}')".format(overly_long_codename)
        )

    def test_create_duplicate_codename(self, source_app):
        with patch.object(source.app.logger, 'error') as logger:
            with source_app.test_client() as app:
                resp = app.get('/generate')
                assert resp.status_code == 200

                # Create a source the first time
                resp = app.post('/create', follow_redirects=True)
                assert resp.status_code == 200

                # Attempt to add the same source
                app.post('/create', follow_redirects=True)
                logger.assert_called_once()
                assert ("Attempt to create a source with duplicate codename"
                        in logger.call_args[0][0])
                assert 'codename' not in session

    def test_lookup(self, source_app):
        """Test various elements on the /lookup page."""
        with source_app.test_client() as app:
            codename = new_codename(app, session)
            resp = app.post('/login', data=dict(codename=codename),
                            follow_redirects=True)
            # redirects to /lookup
            text = resp.data.decode('utf-8')
            assert "public key" in text
            # download the public key
            resp = app.get('/journalist-key')
            text = resp.data.decode('utf-8')
            assert "BEGIN PGP PUBLIC KEY BLOCK" in text

    def test_login_and_logout(self, source_app):
        with source_app.test_client() as app:
            resp = app.get('/login')
            assert resp.status_code == 200
            text = resp.data.decode('utf-8')
            assert "Enter Codename" in text

            codename = new_codename(app, session)
            resp = app.post('/login', data=dict(codename=codename),
                            follow_redirects=True)
            assert resp.status_code == 200
            text = resp.data.decode('utf-8')
            assert "Submit Materials" in text
            assert session['logged_in'] is True

            resp = app.get('/logout', follow_redirects=True)
            assert resp.status_code == 200

            resp = app.post('/login', data=dict(codename='invalid'),
                            follow_redirects=True)
            assert resp.status_code == 200
            text = resp.data.decode('utf-8')
            assert 'Sorry, that is not a recognized codename.' in text
            assert 'logged_in' not in session

            resp = app.post('/login', data=dict(codename=codename),
                            follow_redirects=True)
            assert resp.status_code == 200
            assert session['logged_in'] is True

            resp = app.get('/logout', follow_redirects=True)
            assert 'logged_in' not in session
            assert 'codename' not in session
            text = resp.data.decode('utf-8')
            assert 'Thank you for exiting your session!' in text

    def test_user_must_log_in_for_protected_views(self, source_app):
        with source_app.test_client() as app:
            resp = app.get('/lookup', follow_redirects=True)
            assert resp.status_code == 200
            text = resp.data.decode('utf-8')
            assert "Enter Codename" in text

    def test_login_with_whitespace(self, source_app):
        """
        Test that codenames with leading or trailing whitespace still work"""

        def login_test(app, codename):
            resp = app.get('/login')
            assert resp.status_code == 200
            text = resp.data.decode('utf-8')
            assert "Enter Codename" in text

            resp = app.post('/login', data=dict(codename=codename),
                            follow_redirects=True)
            assert resp.status_code == 200
            text = resp.data.decode('utf-8')
            assert "Submit Materials" in text, text
            assert session['logged_in'] is True

            resp = app.get('/logout', follow_redirects=True)

        with source_app.test_client() as app:
            codename = new_codename(app, session)

        codenames = [
            codename + ' ',
            ' ' + codename + ' ',
            ' ' + codename,
        ]

        for codename_ in codenames:
            with source_app.test_client() as app:
                login_test(app, codename_)

    @staticmethod
    def _dummy_submission(app):
        """
        Helper to make a submission (content unimportant), mostly useful in
        testing notification behavior for a source's first vs. their
        subsequent submissions
        """
        return app.post('/submit', data=dict(
            msg="Pay no attention to the man behind the curtain.",
            fh=(StringIO(''), ''),
        ), follow_redirects=True)

    def test_initial_submission_notification(self, source_app):
        """
        Regardless of the type of submission (message, file, or both), the
        first submission is always greeted with a notification
        reminding sources to check back later for replies.
        """
        with source_app.test_client() as app:
            new_codename(app, session)
            resp = self._dummy_submission(app)
            assert resp.status_code == 200
            text = resp.data.decode('utf-8')
            assert "Thank you for sending this information to us." in text

    def test_submit_message(self, source_app):
        with source_app.test_client() as app:
            new_codename(app, session)
            self._dummy_submission(app)
            resp = app.post('/submit', data=dict(
                msg="This is a test.",
                fh=(StringIO(''), ''),
            ), follow_redirects=True)
            assert resp.status_code == 200
            text = resp.data.decode('utf-8')
            assert "Thanks! We received your message" in text

    def test_submit_empty_message(self, source_app):
        with source_app.test_client() as app:
            new_codename(app, session)
            resp = app.post('/submit', data=dict(
                msg="",
                fh=(StringIO(''), ''),
            ), follow_redirects=True)
            assert resp.status_code == 200
            text = resp.data.decode('utf-8')
            assert "You must enter a message or choose a file to submit." \
                in text

    def test_submit_big_message(self, source_app):
        '''
        When the message is larger than 512KB it's written to disk instead of
        just residing in memory. Make sure the different return type of
        SecureTemporaryFile is handled as well as BytesIO.
        '''
        with source_app.test_client() as app:
            new_codename(app, session)
            self._dummy_submission(app)
            resp = app.post('/submit', data=dict(
                msg="AA" * (1024 * 512),
                fh=(StringIO(''), ''),
            ), follow_redirects=True)
            assert resp.status_code == 200
            text = resp.data.decode('utf-8')
            assert "Thanks! We received your message" in text

    def test_submit_file(self, source_app):
        with source_app.test_client() as app:
            new_codename(app, session)
            self._dummy_submission(app)
            resp = app.post('/submit', data=dict(
                msg="",
                fh=(StringIO('This is a test'), 'test.txt'),
            ), follow_redirects=True)
            assert resp.status_code == 200
            text = resp.data.decode('utf-8')
            assert 'Thanks! We received your document' in text

    def test_submit_both(self, source_app):
        with source_app.test_client() as app:
            new_codename(app, session)
            self._dummy_submission(app)
            resp = app.post('/submit', data=dict(
                msg="This is a test",
                fh=(StringIO('This is a test'), 'test.txt'),
            ), follow_redirects=True)
            assert resp.status_code == 200
            text = resp.data.decode('utf-8')
            assert "Thanks! We received your message and document" in text

    def test_submit_message_with_low_entropy(self, source_app):
        with patch.object(source_app_main, 'async_genkey') as async_genkey:
            with patch.object(source_app_main, 'get_entropy_estimate') \
                    as get_entropy_estimate:
                get_entropy_estimate.return_value = 300

                with source_app.test_client() as app:
                    new_codename(app, session)
                    self._dummy_submission(app)
                    resp = app.post('/submit', data=dict(
                        msg="This is a test.",
                        fh=(StringIO(''), ''),
                    ), follow_redirects=True)
                    assert resp.status_code == 200
                    assert not async_genkey.called

    def test_submit_message_with_enough_entropy(self, source_app):
        with patch.object(source_app_main, 'async_genkey') as async_genkey:
            with patch.object(source_app_main, 'get_entropy_estimate') \
                    as get_entropy_estimate:
                get_entropy_estimate.return_value = 2400

                with source_app.test_client() as app:
                    new_codename(app, session)
                    self._dummy_submission(app)
                    resp = app.post('/submit', data=dict(
                        msg="This is a test.",
                        fh=(StringIO(''), ''),
                    ), follow_redirects=True)
                    assert resp.status_code == 200
                    assert async_genkey.called

    def test_delete_all_successfully_deletes_replies(self, source_app):
        with source_app.app_context():
            journalist, _ = utils.db_helper.init_journalist()
            source, codename = utils.db_helper.init_source()
            utils.db_helper.reply(journalist, source, 1)

        with source_app.test_client() as app:
            resp = app.post('/login', data=dict(codename=codename),
                            follow_redirects=True)
            assert resp.status_code == 200
            resp = app.post('/delete-all', follow_redirects=True)
            assert resp.status_code == 200
            text = resp.data.decode('utf-8')
            assert "All replies have been deleted" in text

    def test_delete_all_replies_already_deleted(self, source_app):
        with source_app.app_context():
            journalist, _ = utils.db_helper.init_journalist()
            source, codename = utils.db_helper.init_source()
            # Note that we are creating the source and no replies

        with source_app.test_client() as app:
            with patch.object(source_app.logger, 'error') as logger:
                resp = app.post('/login', data=dict(codename=codename),
                                follow_redirects=True)
                assert resp.status_code == 200
                resp = app.post('/delete-all', follow_redirects=True)
                assert resp.status_code == 200
                logger.assert_called_once_with(
                    "Found no replies when at least one was expected"
                )

    def test_submit_sanitizes_filename(self, source_app):
        """Test that upload file name is sanitized"""
        insecure_filename = '../../bin/gpg'
        sanitized_filename = 'bin_gpg'

        with patch.object(gzip, 'GzipFile', wraps=gzip.GzipFile) as gzipfile:
            with source_app.test_client() as app:
                new_codename(app, session)
                resp = app.post('/submit', data=dict(
                    msg="",
                    fh=(StringIO('This is a test'), insecure_filename),
                ), follow_redirects=True)
                assert resp.status_code == 200
                gzipfile.assert_called_with(filename=sanitized_filename,
                                            mode=ANY,
                                            fileobj=ANY)


class TestSourceApp(TestCase):

    def create_app(self):
        return source.app

    def setUp(self):
        utils.env.setup()

    def tearDown(self):
        utils.env.teardown()

    def _dummy_submission(self, client):
        """
        Helper to make a submission (content unimportant), mostly useful in
        testing notification behavior for a source's first vs. their
        subsequent submissions
        """
        return client.post('/submit', data=dict(
            msg="Pay no attention to the man behind the curtain.",
            fh=(StringIO(''), ''),
        ), follow_redirects=True)

    def test_tor2web_warning_headers(self):
        resp = self.client.get('/', headers=[('X-tor2web', 'encrypted')])
        self.assertEqual(resp.status_code, 200)
        self.assertIn("You appear to be using Tor2Web.", resp.data)

    def test_tor2web_warning(self):
        resp = self.client.get('/tor2web-warning')
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Why is there a warning about Tor2Web?", resp.data)

    def test_why_use_tor_browser(self):
        resp = self.client.get('/use-tor')
        self.assertEqual(resp.status_code, 200)
        self.assertIn("You Should Use Tor Browser", resp.data)

    def test_why_journalist_key(self):
        resp = self.client.get('/why-journalist-key')
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Why download the journalist's public key?", resp.data)

    def test_metadata_route(self):
        resp = self.client.get('/metadata')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers.get('Content-Type'), 'application/json')
        self.assertEqual(json.loads(resp.data.decode('utf-8')).get(
            'sd_version'), version.__version__)

    @patch('crypto_util.CryptoUtil.hash_codename')
    def test_login_with_overly_long_codename(self, mock_hash_codename):
        """Attempting to login with an overly long codename should result in
        an error, and scrypt should not be called to avoid DoS."""
        with self.client as c:
            resp = c.post('/login', data=dict(codename=overly_long_codename),
                          follow_redirects=True)
            self.assertEqual(resp.status_code, 200)
            self.assertIn("Field must be between 1 and {} "
                          "characters long.".format(Source.MAX_CODENAME_LEN),
                          resp.data)
            self.assertFalse(mock_hash_codename.called,
                             "Called hash_codename for codename w/ invalid "
                             "length")

    @patch('source.app.logger.warning')
    @patch('subprocess.call', return_value=1)
    def test_failed_normalize_timestamps_logs_warning(self, call, logger):
        """If a normalize timestamps event fails, the subprocess that calls
        touch will fail and exit 1. When this happens, the submission should
        still occur, but a warning should be logged (this will trigger an
        OSSEC alert)."""

        with self.client as client:
            new_codename(client, session)
            self._dummy_submission(client)
            resp = client.post('/submit', data=dict(
                msg="This is a test.",
                fh=(StringIO(''), ''),
            ), follow_redirects=True)
            self.assertEqual(resp.status_code, 200)
            self.assertIn("Thanks! We received your message", resp.data)

            logger.assert_called_once_with(
                "Couldn't normalize submission "
                "timestamps (touch exited with 1)"
            )

    @patch('source.app.logger.error')
    def test_source_is_deleted_while_logged_in(self, logger):
        """If a source is deleted by a journalist when they are logged in,
        a NoResultFound will occur. The source should be redirected to the
        index when this happens, and a warning logged."""

        with self.client as client:
            codename = new_codename(client, session)
            resp = client.post('login', data=dict(codename=codename),
                               follow_redirects=True)

            # Now the journalist deletes the source
            filesystem_id = current_app.crypto_util.hash_codename(codename)
            current_app.crypto_util.delete_reply_keypair(filesystem_id)
            source = Source.query.filter_by(filesystem_id=filesystem_id).one()
            db.session.delete(source)
            db.session.commit()

            # Source attempts to continue to navigate
            resp = client.post('/lookup', follow_redirects=True)
            self.assertEqual(resp.status_code, 200)
            self.assertIn('Submit documents for the first time', resp.data)
            self.assertNotIn('logged_in', session.keys())
            self.assertNotIn('codename', session.keys())

        logger.assert_called_once_with(
            "Found no Sources when one was expected: "
            "No row was found for one()")

    def test_login_with_invalid_codename(self):
        """Logging in with a codename with invalid characters should return
        an informative message to the user."""

        invalid_codename = '[]'

        with self.client as c:
            resp = c.post('/login', data=dict(codename=invalid_codename),
                          follow_redirects=True)
            self.assertEqual(resp.status_code, 200)
            self.assertIn("Invalid input.", resp.data)

    def _test_source_session_expiration(self):
        try:
            old_expiration = config.SESSION_EXPIRATION_MINUTES
            has_session_expiration = True
        except AttributeError:
            has_session_expiration = False

        try:
            with self.client as client:
                codename = new_codename(client, session)

                # set the expiration to ensure we trigger an expiration
                config.SESSION_EXPIRATION_MINUTES = -1

                resp = client.post('/login',
                                   data=dict(codename=codename),
                                   follow_redirects=True)
                assert resp.status_code == 200
                resp = client.get('/lookup', follow_redirects=True)

                # check that the session was cleared (apart from 'expires'
                # which is always present and 'csrf_token' which leaks no info)
                session.pop('expires', None)
                session.pop('csrf_token', None)
                assert not session, session
                assert ('You have been logged out due to inactivity' in
                        resp.data.decode('utf-8'))
        finally:
            if has_session_expiration:
                config.SESSION_EXPIRATION_MINUTES = old_expiration
            else:
                del config.SESSION_EXPIRATION_MINUTES

    def test_csrf_error_page(self):
        old_enabled = self.app.config['WTF_CSRF_ENABLED']
        self.app.config['WTF_CSRF_ENABLED'] = True

        try:
            with self.app.test_client() as app:
                resp = app.post(url_for('main.create'))
                self.assertRedirects(resp, url_for('main.index'))

                resp = app.post(url_for('main.create'), follow_redirects=True)
                self.assertIn('Your session timed out due to inactivity',
                              resp.data)
        finally:
            self.app.config['WTF_CSRF_ENABLED'] = old_enabled
