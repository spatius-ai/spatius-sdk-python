from spatius import AvatarSession, OggOpusEncoderConfig, new_avatar_session


def test_spatius_public_package_exports_session_api():
    assert AvatarSession is not None
    assert callable(new_avatar_session)


def test_ogg_opus_encoder_config_defaults_to_voip_application():
    assert OggOpusEncoderConfig().application == "voip"
