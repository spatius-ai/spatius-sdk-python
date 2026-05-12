import os
import unittest
from datetime import datetime, timedelta, timezone
from typing import cast

from spatius import (
    AvatarSDKError,
    AvatarSDKErrorCode,
    LiveKitEgressConfig,
    SessionTokenError,
    new_avatar_session,
)


def _require_env(*names: str) -> dict[str, str]:
    values: dict[str, str] = {}
    missing: list[str] = []
    for name in names:
        value = os.getenv(name, "").strip()
        if not value:
            missing.append(name)
        else:
            values[name] = value
    if missing:
        raise unittest.SkipTest("Missing required e2e env vars: " + ", ".join(missing))
    return values


def _endpoint_kwargs() -> dict[str, str]:
    return {
        "region": os.getenv("SPATIUS_E2E_REGION", "us-west").strip() or "us-west",
        "console_endpoint_url": os.getenv("SPATIUS_E2E_CONSOLE_ENDPOINT", "").strip(),
        "ingress_endpoint_url": os.getenv("SPATIUS_E2E_INGRESS_ENDPOINT", "").strip(),
    }


@unittest.skipUnless(
    os.getenv("SPATIUS_RUN_E2E") == "1",
    "Set SPATIUS_RUN_E2E=1 to run end-to-end network tests",
)
class TestE2EErrors(unittest.IsolatedAsyncioTestCase):
    async def test_start_with_bogus_credentials_surfaces_structured_error(self):
        session = new_avatar_session(
            **_endpoint_kwargs(),
            api_key="unused-for-this-test",
            avatar_id="e2e-invalid-avatar",
            app_id="e2e-invalid-app",
            use_query_auth=False,
        )
        session._session_token = "e2e-invalid-session-token"

        try:
            with self.assertRaises(AvatarSDKError) as cm:
                await session.start()
        finally:
            await session.close()

        err = cm.exception
        self.assertEqual(err.code, AvatarSDKErrorCode.sessionTokenInvalid)
        self.assertEqual(err.phase, "websocket_connect")
        self.assertEqual(err.http_status, 400)
        self.assertEqual(err.server_detail, "Invalid session token")
        self.assertIn("Invalid session token", err.message)
        self.assertEqual(err.raw_body, '{"message":"Invalid session token"}\n')

    async def test_start_with_missing_avatar_surfaces_avatar_not_found(self):
        env = _require_env(
            "SPATIUS_E2E_API_KEY",
            "SPATIUS_E2E_APP_ID",
        )

        missing_avatar_id = os.getenv(
            "SPATIUS_E2E_MISSING_AVATAR_ID",
            "spatius-e2e-missing-avatar-404",
        ).strip()

        session = new_avatar_session(
            api_key=env["SPATIUS_E2E_API_KEY"],
            app_id=env["SPATIUS_E2E_APP_ID"],
            **_endpoint_kwargs(),
            avatar_id=missing_avatar_id,
            expire_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )

        try:
            await session.init()
            with self.assertRaises(AvatarSDKError) as cm:
                await session.start()
        except SessionTokenError as exc:
            raise AssertionError(
                "Expected valid e2e credentials, but session token creation failed"
            ) from exc
        finally:
            await session.close()

        err = cm.exception
        self.assertEqual(err.code, AvatarSDKErrorCode.avatarNotFound)
        self.assertEqual(err.phase, "websocket_connect")
        self.assertEqual(err.http_status, 404)
        self.assertIsNotNone(err.server_detail)
        self.assertIn("Avatar not found", cast(str, err.server_detail))

    async def test_start_with_invalid_livekit_token_surfaces_invalid_egress_config(
        self,
    ):
        env = _require_env(
            "SPATIUS_E2E_API_KEY",
            "SPATIUS_E2E_APP_ID",
            "SPATIUS_E2E_AVATAR_ID",
            "SPATIUS_E2E_LIVEKIT_URL",
        )

        session = new_avatar_session(
            api_key=env["SPATIUS_E2E_API_KEY"],
            app_id=env["SPATIUS_E2E_APP_ID"],
            **_endpoint_kwargs(),
            avatar_id=env["SPATIUS_E2E_AVATAR_ID"],
            expire_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            livekit_egress=LiveKitEgressConfig(
                url=env["SPATIUS_E2E_LIVEKIT_URL"],
                api_token="spatius-e2e-invalid-livekit-token",
                room_name=os.getenv(
                    "SPATIUS_E2E_LIVEKIT_ROOM_NAME",
                    "spatius-e2e-invalid-token-room",
                ).strip(),
                publisher_id=os.getenv(
                    "SPATIUS_E2E_LIVEKIT_PUBLISHER_ID",
                    "spatius-e2e-invalid-token-publisher",
                ).strip(),
            ),
        )

        try:
            await session.init()
            with self.assertRaises(AvatarSDKError) as cm:
                await session.start()
        except SessionTokenError as exc:
            raise AssertionError(
                "Expected valid e2e credentials, but session token creation failed"
            ) from exc
        finally:
            await session.close()

        err = cm.exception
        print(err.message)
        self.assertEqual(err.code, AvatarSDKErrorCode.invalidEgressConfig)
        self.assertEqual(err.phase, "websocket_handshake")
        self.assertEqual(err.server_code, "16")
        self.assertIsNotNone(err.server_detail)
        self.assertIn("unauthorized", cast(str, err.server_detail).lower())
