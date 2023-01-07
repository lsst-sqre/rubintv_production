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
            "startracker_raw",
            "startracker_wide_raw",
            "startracker_analysis",
            "startracker_wide_analysis",
            "startracker_metadata",
            "ts8_noise_map",
            "ts8_focal_plane_mosiac",
            ]

PREFIXES = {chan: chan.replace('_', '-') for chan in CHANNELS}
