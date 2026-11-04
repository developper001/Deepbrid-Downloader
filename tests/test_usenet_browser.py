from tests._test_support import (
    FakeResponse,
    Path,
    UsenetBrowserSession,
    UsenetFinderError,
    json,
    patch,
    tempfile,
    unittest,
)

class UsenetBrowserSessionTests(unittest.TestCase):
    def test_browser_connection_check_requires_a_deepbrid_tab(self) -> None:
        session = UsenetBrowserSession(Path("unused"))
        with (
            patch.object(session, "_debug_port", return_value=9222),
            patch.object(
                session,
                "_find_finder_target",
                return_value={"url": "https://www.deepbrid.com/login"},
            ),
        ):
            self.assertIn("connection is working", session.test_connection())

        with (
            patch.object(session, "_debug_port", return_value=None),
            patch.object(session, "_find_browser", return_value=None),
            self.assertRaisesRegex(UsenetFinderError, "Chrome or Microsoft Edge was not found"),
        ):
            session.test_connection()
        with (
            patch.object(session, "_debug_port", return_value=None),
            patch.object(session, "_find_browser", return_value="chrome"),
            self.assertRaisesRegex(UsenetFinderError, "not connected"),
        ):
            session.test_connection()

    def test_missing_browser_error_explains_requirement_and_install_options(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            session = UsenetBrowserSession(Path(temporary_directory))
            with (
                patch.object(session, "_debug_port", return_value=None),
                patch.object(session, "_close_legacy_automated_browser", return_value=False),
                patch.object(session, "_find_browser", return_value=None),
                patch("src.usenet_browser.sys.platform", "linux"),
                self.assertRaisesRegex(UsenetFinderError, "requires Google Chrome or Microsoft Edge") as error,
            ):
                session.open()
        self.assertIn("https://www.google.com/chrome/", str(error.exception))
        self.assertIn("https://www.microsoft.com/edge/download", str(error.exception))
        self.assertIn("Ubuntu or Debian", str(error.exception))

    def test_launch_uses_a_separate_profile_and_loopback_debugger_on_fixed_port(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            profile = Path(temporary_directory) / "browser-profile"
            session = UsenetBrowserSession(profile, browser_path="C:/Chrome/chrome.exe")
            with (
                patch.object(session, "_debug_port", return_value=None),
                patch.object(session, "_find_free_port", return_value=9225),
                patch("src.usenet_browser.subprocess.Popen") as popen,
            ):
                session.open()
            args = popen.call_args.args[0]
            self.assertIn(f"--user-data-dir={profile}", args)
            self.assertIn("--remote-debugging-port=9225", args)
            self.assertIn("--remote-debugging-address=127.0.0.1", args)
            self.assertEqual(args[-1], "https://www.deepbrid.com/login")
            self.assertEqual((profile / "DeepbridDevToolsPort").read_text(), "9225")

    def test_debug_port_comes_from_the_application_port_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            profile = Path(temporary_directory)
            (profile / "DeepbridDevToolsPort").write_text("9225", encoding="ascii")
            session = UsenetBrowserSession(profile)
            with patch(
                "src.usenet_browser.urllib.request.urlopen",
                return_value=FakeResponse(
                    200,
                    {},
                    b'{"Browser":"Chrome/154.0.0.0"}',
                ),
            ) as open_url:
                self.assertEqual(session._debug_port(), 9225)
            self.assertEqual(
                open_url.call_args.args[0],
                "http://127.0.0.1:9225/json/version",
            )

    def test_fetch_runs_inside_browser_and_never_exports_cookie_header(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            messages: list[str] = []
            session = UsenetBrowserSession(Path(temporary_directory), log=messages.append)
            target = {
                "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/one"
            }
            response = {
                "status": 200,
                "contentType": "application/json",
                "body": '{"results":[],"hasMore":false}',
            }
            with (
                patch.object(session, "_wait_for_debug_port", return_value=9222),
                patch.object(session, "_find_finder_target", return_value=target),
                patch.object(UsenetBrowserSession, "_evaluate", return_value=response) as evaluate,
            ):
                payload = session.request_json({"do": "search", "q": "test", "limit": "15"})

            self.assertEqual(payload, {"results": [], "hasMore": False})
            expression = evaluate.call_args.args[1]
            self.assertIn("credentials:'same-origin'", expression)
            self.assertNotIn("Cookie", expression)
            self.assertNotIn("token", "\n".join(messages))

    def test_cdp_evaluation_uses_page_runtime_and_closes_local_websocket(self) -> None:
        class FakeWebSocket:
            def __init__(self):
                self.sent: list[str] = []
                self.closed = False

            def send(self, payload: str) -> None:
                self.sent.append(payload)

            def recv(self) -> str:
                return json.dumps(
                    {
                        "id": 1,
                        "result": {
                            "result": {
                                "value": {
                                    "status": 200,
                                    "contentType": "application/json",
                                    "body": '{"results":[],"hasMore":false}',
                                }
                            }
                        },
                    }
                )

            def close(self) -> None:
                self.closed = True

        connection = FakeWebSocket()
        with patch(
            "src.usenet_browser.websocket.create_connection",
            return_value=connection,
        ) as connect:
            response = UsenetBrowserSession._evaluate(
                "ws://127.0.0.1:9222/devtools/page/test",
                "fetch('/usenet-finder')",
            )

        self.assertEqual(response["status"], 200)
        self.assertTrue(connection.closed)
        self.assertEqual(connect.call_args.kwargs["suppress_origin"], True)
        command = json.loads(connection.sent[0])
        self.assertEqual(command["method"], "Runtime.evaluate")
        self.assertTrue(command["params"]["awaitPromise"])

    def test_cdp_refuses_remote_debugging_hosts(self) -> None:
        with self.assertRaisesRegex(UsenetFinderError, "non-local"):
            UsenetBrowserSession._evaluate(
                "ws://attacker.example/devtools/page/test",
                "document.cookie",
            )
