from tests._test_support import (
    APP_USER_AGENT,
    BytesIO,
    DeepbridClient,
    DeepbridError,
    FakeResponse,
    Path,
    json,
    patch,
    tempfile,
    unittest,
    urllib,
)
from src.remote_jobs import UsenetUpload, UsenetUploadFile

class DeepbridDiagnosticsTests(unittest.TestCase):
    def test_torrent_upload_uses_authenticated_multipart_api(self) -> None:
        response = FakeResponse(200, {}, b'{"error":0,"id":14648}')
        with patch(
            "src.deepbrid_client.urllib.request.urlopen",
            return_value=response,
        ) as open_url:
            job = DeepbridClient("test-key").submit_torrent_file(
                b"torrent-metainfo",
                "bundle.torrent",
            )

        request = open_url.call_args.args[0]
        self.assertEqual(request.full_url, "https://www.deepbrid.com/api/v1/torrents/add")
        self.assertEqual(request.get_header("Authorization"), "Bearer test-key")
        self.assertIn("multipart/form-data", request.get_header("Content-type"))
        self.assertIn(b'name="torrent_file"; filename="bundle.torrent"', request.data)
        self.assertIn(b"torrent-metainfo", request.data)
        self.assertEqual(job.id, "14648")
        self.assertEqual(job.name, "bundle.torrent")

    def test_torrent_status_parses_download_links_and_progress(self) -> None:
        response = FakeResponse(
            200,
            {},
            (
                b'{"error":0,"id":"14648","filename":"bundle.torrent",'
                b'"progress":100,"seeders":2,"speed":"0.00 MB/s",'
                b'"links":["https://deepbrid.example/mytorrents?file=secret"],'
                b'"status":"downloaded"}'
            ),
        )
        with patch(
            "src.deepbrid_client.urllib.request.urlopen",
            return_value=response,
        ) as open_url:
            job = DeepbridClient("test-key").get_job("14648")

        request = open_url.call_args.args[0]
        self.assertTrue(request.full_url.endswith("/api/v1/torrents/info?id=14648"))
        self.assertEqual(job.status, "downloaded")
        self.assertEqual(job.progress, 100.0)
        self.assertEqual(job.seeders, 2)
        self.assertEqual(job.files[0].download_url, "https://deepbrid.example/mytorrents?file=secret")

    def test_nzb_file_upload_uses_authenticated_multipart_api(self) -> None:
        response = FakeResponse(
            200,
            {},
            b'{"error":0,"id":1024,"files":[{"filename":"movie.mkv",'
            b'"filesize":1234,"download_url":"https://usenet.example/file/movie.mkv"}]}',
        )
        with patch(
            "src.deepbrid_client.urllib.request.urlopen",
            return_value=response,
        ) as open_url:
            upload = DeepbridClient("test-key").upload_nzb_file(b"<nzb/>", "test.nzb")

        request = open_url.call_args.args[0]
        self.assertEqual(request.full_url, "https://www.deepbrid.com/api/v1/usenet/add")
        self.assertEqual(request.get_header("Authorization"), "******")
        self.assertIn("multipart/form-data", request.get_header("Content-type"))
        self.assertIn(b'name="nzb_file"; filename="test.nzb"', request.data)
        self.assertIn(b"<nzb/>", request.data)
        self.assertEqual(
            upload,
            UsenetUpload(
                "1024",
                "test.nzb",
                (
                    UsenetUploadFile(
                        "movie.mkv",
                        "https://usenet.example/file/movie.mkv",
                        1234,
                    ),
                ),
            ),
        )

    def test_nzb_url_upload_is_form_encoded_and_marks_unreachable_files(self) -> None:
        response = FakeResponse(
            200,
            {},
            b'{"error":0,"id":1025,"title":"release",'
            b'"files":[{"name":"movie.mkv","size":1234,"link":"https://usenet.example/file",'
            b'"unreachable":null},{"name":"missing.mkv","size":42,"link":"",'
            b'"unreachable":"missing_volume"}]}',
        )
        with patch(
            "src.deepbrid_client.urllib.request.urlopen",
            return_value=response,
        ) as open_url:
            upload = DeepbridClient("test-key").upload_nzb_url(
                "https://files.example/release.nzb?token=a&part=1"
            )

        request = open_url.call_args.args[0]
        self.assertEqual(request.full_url, "https://www.deepbrid.com/api/v1/usenet/add")
        self.assertEqual(request.get_header("Content-type"), "application/x-www-form-urlencoded")
        self.assertIn(
            b"nzb_url=https%3A%2F%2Ffiles.example%2Frelease.nzb%3Ftoken%3Da%26part%3D1",
            request.data,
        )
        self.assertEqual(upload.title, "release")
        self.assertEqual(upload.files[0].size, 1234)
        self.assertEqual(upload.files[1].download_url, "")
        self.assertEqual(upload.files[1].unreachable, "missing_volume")

    def test_nzb_url_rejects_non_http_urls(self) -> None:
        with self.assertRaisesRegex(ValueError, "valid HTTP or HTTPS"):
            DeepbridClient("test-key").upload_nzb_url("file:///private/release.nzb")

    def test_nzb_upload_reports_api_errors(self) -> None:
        response = FakeResponse(
            200,
            {},
            b'{"error":12,"message":"Could not process NZB"}',
        )
        with patch("src.deepbrid_client.urllib.request.urlopen", return_value=response):
            with self.assertRaisesRegex(DeepbridError, "Could not process NZB"):
                DeepbridClient("test-key").upload_nzb_url("https://files.example/release.nzb")

    def test_unsupported_filehost_response_is_not_retryable(self) -> None:
        response = FakeResponse(
            200,
            {},
            b'{"error":10,"message":"Filehoster not supported"}',
        )
        with patch("src.deepbrid_client.urllib.request.urlopen", return_value=response):
            with self.assertRaisesRegex(DeepbridError, "Filehoster not supported") as raised:
                DeepbridClient("test-key").generate_link("https://unsupported.example/file")

        self.assertFalse(raised.exception.retryable)
        self.assertTrue(raised.exception.skip_queue_item)

    def test_valid_api_key_account_response_is_accepted(self) -> None:
        response = FakeResponse(200, {}, b'{"type":"premium","error":0}')
        with patch("src.deepbrid_client.urllib.request.urlopen", return_value=response) as open_url:
            DeepbridClient("valid-test-key").validate_api_key()
        request = open_url.call_args.args[0]
        self.assertEqual(request.full_url, "https://www.deepbrid.com/api/v1/user")
        self.assertEqual(request.get_header("Authorization"), "Bearer valid-test-key")

    def test_api_key_validation_logs_redacted_request_and_result(self) -> None:
        secret = "sensitive-test-api-key"
        messages: list[str] = []
        response = FakeResponse(200, {}, b'{"type":"premium","error":0}')
        with patch("src.deepbrid_client.urllib.request.urlopen", return_value=response):
            DeepbridClient(secret, log=messages.append).validate_api_key()

        self.assertTrue(any("curl -X GET" in message for message in messages))
        self.assertTrue(any("Authorization: Bearer <redacted>" in message for message in messages))
        self.assertIn("API-key validation response: HTTP 200.", messages)
        self.assertIn("API-key validation succeeded.", messages)
        self.assertNotIn(secret, "\n".join(messages))

    def test_invalid_api_key_is_reported_as_non_retryable_401(self) -> None:
        error = urllib.error.HTTPError(
            "https://www.deepbrid.com/api/v1/user",
            401,
            "Unauthorized",
            {},
            BytesIO(b'{"error":401,"message":"Authentication required."}'),
        )
        with patch("src.deepbrid_client.urllib.request.urlopen", side_effect=error):
            with self.assertRaises(DeepbridError) as raised:
                DeepbridClient("bad-key").validate_api_key()
        self.assertEqual(raised.exception.status_code, 401)
        self.assertFalse(raised.exception.retryable)

    def test_fetch_hosts_accepts_current_string_array_and_legacy_status_map(self) -> None:
        current = FakeResponse(200, {}, b'["ddownload","mega"]')
        with patch("src.deepbrid_client.urllib.request.urlopen", return_value=current):
            current_hosts = DeepbridClient.fetch_hosts()
        self.assertEqual(current_hosts, {"ddownload": "supported", "mega": "supported"})

        legacy = FakeResponse(200, {}, b'[{"mega.nz":"up"},{"ddownload.com":"down (today)"}]')
        with patch("src.deepbrid_client.urllib.request.urlopen", return_value=legacy):
            legacy_hosts = DeepbridClient.fetch_hosts()
        self.assertEqual(legacy_hosts["mega.nz"], "up")
        self.assertEqual(legacy_hosts["ddownload.com"], "down (today)")

    def test_fetch_hosts_uses_api_key_for_live_status(self) -> None:
        response = FakeResponse(200, {}, b'[{"ddownload.com":"up"},{"1fichier.com":"down (today)"}]')
        with patch("src.deepbrid_client.urllib.request.urlopen", return_value=response) as open_url:
            hosts = DeepbridClient.fetch_hosts("test-api-key")

        self.assertEqual(hosts, {"ddownload.com": "up", "1fichier.com": "down (today)"})
        request = open_url.call_args.args[0]
        self.assertEqual(request.full_url, "https://www.deepbrid.com/api/v1/hosts")
        self.assertEqual(request.get_header("Authorization"), "Bearer test-api-key")

    def test_fetch_hosts_preserves_http_401_status(self) -> None:
        error = urllib.error.HTTPError(
            "https://www.deepbrid.com/api/v1/hosts",
            401,
            "Unauthorized",
            {},
            BytesIO(b"API key rejected"),
        )
        with patch("src.deepbrid_client.urllib.request.urlopen", side_effect=error):
            with self.assertRaises(DeepbridError) as raised:
                DeepbridClient.fetch_hosts("invalid-test-key")
        self.assertEqual(raised.exception.status_code, 401)

    def test_fetch_host_limits_formats_daily_links_and_bandwidth(self) -> None:
        response = FakeResponse(
            200,
            {},
            b'{"error":0,"reset":"daily","hosters":['
            b'{"domain":"ddownload.com","type":"links","limit":5,"used":3,"remaining":2},'
            b'{"domain":"mega.nz","type":"bandwidth","remaining":5368709120,"remaining_str":"5.00 GB"}]}',
        )
        with patch("src.deepbrid_client.urllib.request.urlopen", return_value=response) as open_url:
            limits = DeepbridClient.fetch_host_limits("test-key")
        self.assertEqual(limits["ddownload.com"], "2 / 5 links")
        self.assertEqual(limits["mega.nz"], "5.00 GB remaining")
        self.assertEqual(open_url.call_args.args[0].full_url, "https://www.deepbrid.com/api/v1/user/limits")

    def test_fetch_host_limits_preserves_json_401_status(self) -> None:
        response = FakeResponse(200, {}, b'{"error":401,"message":"Authentication required."}')
        with patch("src.deepbrid_client.urllib.request.urlopen", return_value=response):
            with self.assertRaisesRegex(DeepbridError, "Authentication required") as raised:
                DeepbridClient.fetch_host_limits("invalid-test-key")
        self.assertEqual(raised.exception.status_code, 401)

    def test_fetch_host_limits_accepts_live_hoster_field(self) -> None:
        response = FakeResponse(
            200,
            {},
            b'{"error":0,"hosters":[{"hoster":"ddownload.com","type":"bandwidth",'
            b'"remaining_str":"4.00 GB"}]}',
        )
        with patch("src.deepbrid_client.urllib.request.urlopen", return_value=response):
            limits = DeepbridClient.fetch_host_limits("test-key")
        self.assertEqual(limits, {"ddownload.com": "4.00 GB remaining"})

    def test_fetch_host_limits_splits_comma_separated_aliases(self) -> None:
        response = FakeResponse(
            200,
            {},
            b'{"error":0,"hosters":[{"hoster":"ddl.to,ddownload.com","type":"bandwidth",'
            b'"remaining_str":"2.78 GB"}]}',
        )
        with patch("src.deepbrid_client.urllib.request.urlopen", return_value=response):
            limits = DeepbridClient.fetch_host_limits("test-key")
        self.assertEqual(limits["ddownload.com"], "2.78 GB remaining")
        self.assertEqual(limits["ddl.to"], "2.78 GB remaining")

    def test_empty_host_response_is_an_explicit_error(self) -> None:
        empty = FakeResponse(200, {}, b"[]")
        with patch("src.deepbrid_client.urllib.request.urlopen", return_value=empty):
            with self.assertRaisesRegex(DeepbridError, "no hosts.*HTTP 200"):
                DeepbridClient.fetch_hosts()

    def test_http_error_logs_status_body_and_redacted_curl(self) -> None:
        api_key = "secret-test-key"
        body = b'{"error":401,"message":"Authentication required."}'
        error = urllib.error.HTTPError(
            "https://www.deepbrid.com/api/v1/generate/link",
            401,
            "Unauthorized",
            {},
            BytesIO(body),
        )
        messages: list[str] = []
        client = DeepbridClient(api_key, log=messages.append)

        with patch("src.deepbrid_client.urllib.request.urlopen", side_effect=error) as open_url:
            with self.assertRaisesRegex(DeepbridError, "HTTP 401") as raised:
                client.generate_link("https://example.com/file.zip")

        combined = "\n".join(messages) + "\n" + str(raised.exception)
        self.assertIn("HTTP 401", combined)
        self.assertIn("Authentication required", combined)
        self.assertIn("curl -X POST", combined)
        self.assertIn("Bearer <REDACTED>", combined)
        self.assertNotIn(api_key, combined)
        request = open_url.call_args.args[0]
        self.assertEqual(request.get_header("User-agent"), APP_USER_AGENT)

    def test_successful_response_does_not_log_temporary_download_url(self) -> None:
        download_url = "https://premium-dl.deepbrid.com/d/private-token"
        body = (
            '{"error":0,"link":"'
            + download_url
            + '","filename":"file.zip"}'
        ).encode()
        response = FakeResponse(200, {}, body)
        messages: list[str] = []
        client = DeepbridClient("secret-test-key", log=messages.append)

        with patch("src.deepbrid_client.urllib.request.urlopen", return_value=response):
            client.generate_link("https://example.com/file.zip")

        self.assertNotIn(download_url, "\n".join(messages))
        self.assertIn("<DOWNLOAD_LINK_REDACTED>", "\n".join(messages))

    def test_premium_link_generation_does_not_log_source_url(self) -> None:
        source_url = "https://usenet.example/download/123?ticket=private-value"
        error = urllib.error.HTTPError(
            "https://www.deepbrid.com/api/v1/generate/link",
            403,
            "Forbidden",
            {},
            BytesIO(
                json.dumps(
                    {
                        "message": f"Cannot process {source_url}",
                        "source": urllib.parse.quote_plus(source_url),
                    }
                ).encode()
            ),
        )
        messages: list[str] = []
        with patch("src.deepbrid_client.urllib.request.urlopen", side_effect=error):
            with self.assertRaises(DeepbridError) as raised:
                DeepbridClient("secret-test-key", log=messages.append).generate_link(source_url)

        combined = "\n".join(messages) + str(raised.exception)
        self.assertNotIn(source_url, combined)
        self.assertNotIn(urllib.parse.quote_plus(source_url), combined)
        self.assertIn("<SOURCE_URL_REDACTED>", combined)

    def test_invalid_json_logs_http_status_and_body(self) -> None:
        response = FakeResponse(200, {}, b"upstream temporarily broken")
        messages: list[str] = []
        client = DeepbridClient("secret-test-key", log=messages.append)

        with patch("src.deepbrid_client.urllib.request.urlopen", return_value=response):
            with self.assertRaisesRegex(DeepbridError, "HTTP 200") as raised:
                client.generate_link("https://example.com/file.zip")

        combined = "\n".join(messages) + "\n" + str(raised.exception)
        self.assertIn("upstream temporarily broken", combined)
        self.assertIn("curl -X POST", combined)
        self.assertNotIn("secret-test-key", combined)

    def test_download_http_error_includes_redacted_curl_template(self) -> None:
        download_url = "https://premium-dl.deepbrid.com/d/private-token"
        error = urllib.error.HTTPError(
            download_url,
            503,
            "Unavailable",
            {},
            BytesIO(b"upstream offline"),
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            with patch("src.deepbrid_client.urllib.request.urlopen", side_effect=error):
                with self.assertRaisesRegex(DeepbridError, "HTTP 503") as raised:
                    DeepbridClient("secret-test-key").download(
                        download_url,
                        "file.bin",
                        Path(temporary_directory),
                        lambda: False,
                        lambda *_: None,
                    )

        self.assertEqual(raised.exception.status_code, 503)
        message = str(raised.exception)
        self.assertIn("upstream offline", message)
        self.assertIn("curl -L", message)
        self.assertIn("<TEMPORARY_DOWNLOAD_URL_REDACTED>", message)
        self.assertNotIn("private-token", message)

    def test_cloudflare_owner_block_is_not_retryable(self) -> None:
        body = b'{"error_code":1010,"error_name":"browser_signature_banned","retryable":false,"owner_action_required":true}'
        error = urllib.error.HTTPError(
            "https://www.deepbrid.com/api/v1/generate/link",
            403,
            "Forbidden",
            {},
            BytesIO(body),
        )
        with patch("src.deepbrid_client.urllib.request.urlopen", side_effect=error):
            with self.assertRaises(DeepbridError) as raised:
                DeepbridClient("test-key").generate_link("https://supported.example/file")

        self.assertFalse(raised.exception.retryable)
        self.assertEqual(raised.exception.status_code, 403)
