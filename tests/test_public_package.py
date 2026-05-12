from spatius import AvatarSession, new_avatar_session


def test_spatius_public_package_exports_session_api():
    assert AvatarSession is not None
    assert callable(new_avatar_session)
