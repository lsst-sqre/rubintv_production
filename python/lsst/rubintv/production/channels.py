CHANNELS = ["summit_imexam",
            "summit_specexam",
            "auxtel_mount_torques",
            "auxtel_monitor",
            "all_sky_current",
            "all_sky_movies",
            "auxtel_metadata",
            "auxtel_metadata_creator",
            "auxtel_movies",
            "auxtel_isr_runner",
            "auxtel_night_reports",
            "startracker_night_reports",
            "startracker_raw",
            "startracker_wide_raw",
            "startracker_fast_raw",
            "startracker_analysis",
            "startracker_wide_analysis",
            "startracker_fast_analysis",
            "startracker_metadata",
            "ts8_noise_map",
            "ts8_focal_plane_mosaic",
            "ts8_metadata",
            "comcam_noise_map",
            "comcam_focal_plane_mosaic",
            "comcam_metadata",
            "slac_lsstcam_noise_map",
            "slac_lsstcam_focal_plane_mosaic",
            "slac_lsstcam_metadata",
            "tma_mount_motion_profile",
            "tma_metadata",
            ]

PREFIXES = {chan: chan.replace('_', '-') for chan in CHANNELS}
