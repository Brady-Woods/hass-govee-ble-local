"""Constants for the Govee H60A6 integration."""

DOMAIN = "govee_h60a6"

# BLE local-name prefix these lights advertise (e.g. "GVH60A67457"), used
# for discovery matching in both the manifest and the manual config flow.
DEVICE_NAME_PREFIX = "GVH60A6"

WRITE_CHAR_UUID = "00010203-0405-0607-0809-0a0b0c0d2b11"
NOTIFY_CHAR_UUID = "00010203-0405-0607-0809-0a0b0c0d2b10"

PSK = b"MakingLifeSmarte"

ZONE_LOWER = 0
ZONE_UPPER = 1

# Confirmed from real captured "max warmth" / "max cool" commands
MIN_COLOR_TEMP_KELVIN = 2700
MAX_COLOR_TEMP_KELVIN = 6500

# name -> (scene_id_byte0, scene_id_byte1), from opcode `33 05 04 <b0> <b1>`
SCENES = {
    "Sunrise": (0x83, 0x4A),
    "Graffiti": (0x84, 0x4A),
    "Rainbow": (0x85, 0x4A),
    "Forest": (0x81, 0x4A),
    "Aurora": (0x82, 0x4A),
    "Firefly": (0x86, 0x4A),
    "Flower Field": (0x87, 0x4A),
    "Ocean": (0x88, 0x4A),
    "Volcano": (0x89, 0x4A),
    "Dandelion": (0x8A, 0x4A),
    "Desert": (0x8B, 0x4A),
    "Spring": (0x8C, 0x4A),
    "Summer": (0x8D, 0x4A),
    "Fall": (0x8E, 0x4A),
    "Winter": (0x8F, 0x4A),
    "green Wheat Field": (0x90, 0x4A),
    "Corn Field": (0x91, 0x4A),
    "Wave": (0x92, 0x4A),
    "grassland": (0x93, 0x4A),
    "Field": (0x94, 0x4A),
    "Christmas": (0x95, 0x4A),
    "Christmas B": (0x96, 0x4A),
    "Halloween": (0x97, 0x4A),
    "Halloween B": (0x98, 0x4A),
    "Father's Day": (0x99, 0x4A),
    "Mother's Day": (0x9A, 0x4A),
    "Easter": (0x9B, 0x4A),
    "Valentine's Day": (0x9C, 0x4A),
    "Carnival": (0x9D, 0x4A),
    "Mother's Hug": (0x01, 0x5A),
    "Sweet": (0x9E, 0x4A),
    "Mild": (0x9F, 0x4A),
    "Colorful Clouds": (0xA0, 0x4A),
    "Ice Drinks": (0xA1, 0x4A),
    "Skyline": (0xA2, 0x4A),
    "Unspoken Love": (0xA3, 0x4A),
    "Sunshine": (0xA4, 0x4A),
    "Care": (0xA5, 0x4A),
    "Night": (0xA6, 0x4A),
    "Dreamlike": (0xA7, 0x4A),
    "Clear Sky": (0xA8, 0x4A),
    "Stream": (0xA9, 0x4A),
    "Peach": (0xAA, 0x4A),
    "Blessing": (0xAB, 0x4A),
    "Herbal": (0xAC, 0x4A),
    "Fascination": (0xAD, 0x4A),
    "Soothing": (0xAE, 0x4A),
    "Dusk": (0xAF, 0x4A),
    "Passion": (0xB0, 0x4A),
    "Blue": (0xB1, 0x4A),
    "Gleam": (0xB2, 0x4A),
    "Red Mist": (0xB3, 0x4A),
    "Haystack": (0xB4, 0x4A),
    "Rustling Leaves": (0xD3, 0x4A),
    "White Light": (0x63, 0x00),
    "Illumination": (0x19, 0x00),
    "Night Light": (0xD4, 0x4A),
    "Morning": (0xD5, 0x4A),
    "Afternoon": (0xD6, 0x4A),
    "Twilight": (0xD7, 0x4A),
    "Sunset": (0xD8, 0x4A),
    "Refreshing": (0xD9, 0x4A),
    "Sky": (0xDA, 0x4A),
    "Meditation": (0xDC, 0x4A),
    "Sunset Glow": (0xDD, 0x4A),
    "Rainbow Circle": (0x49, 0x4B),
    "Motunui": (0x8E, 0x5C),
    "Heart of the Island": (0x8F, 0x5C),
    "Wayfinding": (0x90, 0x5C),
}

SCENE_ID_TO_NAME = {v: k for k, v in SCENES.items()}

# Scenes confirmed (via real-device, human-observed testing - not just status
# query, which is known unreliable here) to fail to render correctly over
# BLE, despite being genuine, officially-supported scenes for this device
# (independently confirmed via Govee's authenticated Cloud API - see
# PROTOCOL.md 6.5). Two distinct, still-unresolved root causes - see
# PROTOCOL.md 6.3.1/6.6/10:
#   - "0xFF placeholder" scenes: Aurora, Dandelion, Desert, Fall,
#     Green Wheat Field, Volcano. Size-independent.
#   - Oversized scenes: Ocean, Winter (largest scenes in the library;
#     Ocean additionally causes an outright BLE disconnect).
# Hidden from the effect picker so selecting one doesn't produce a
# silent no-op or a scene that visibly fails - see README.md's "Known
# issues" section for what additional testing would be needed to lift
# this and turn these back on.
BROKEN_SCENE_NAMES = {
    "aurora",
    "dandelion",
    "desert",
    "fall",
    "green wheat field",
    "volcano",
    "ocean",
    "winter",
}

# 60s (not 30s): with 4 lights sharing one adapter, every poll opens a BLE
# connection that locks the device's single slot and competes with the
# others plus HA's own background scanning. Halving the poll frequency
# roughly halves that contention, which was causing frequent dropped status
# chunks (notably the per-segment chunks, leaving segments unavailable).
# These are near-static ceiling lights; 60s freshness is plenty.
POLL_INTERVAL_SECONDS = 60
