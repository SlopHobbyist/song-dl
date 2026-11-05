"""Swaps the example credentials in config.yaml for random ones.

config.yaml.example ships placeholders (YourUsername, YourPassword,
change-me-please). Rather than every install sharing them, each start replaces
any still there with random values and saves them back into config.yaml, so
the admin can read them there. The rest of the file, comments included, is
left as it was.

- Soulseek username: words and a number, like HorseTomatoPunctually4256.
  Soulseek makes the account the first time an unused name logs in.
- Passwords: 32 random letters and digits.
"""

import re
import secrets
import string
from pathlib import Path

import yaml

from .config import PLACEHOLDER_PASSWORDS, PLACEHOLDER_USERNAMES, ConfigError

MAX_USERNAME = 30   # Soulseek's limit
PASSWORD_LENGTH = 32
PASSWORD_CHARS = string.ascii_letters + string.digits   # nothing YAML or a shell would need escaped

NOUNS = '''
    Acorn Almond Anchor Apple Badger Bagel Banana Barrel Basket Beacon Beaver Biscuit Bison Blanket Bottle
    Boulder Breeze Bucket Butter Button Cabbage Cactus Camel Candle Canoe Carrot Castle Cedar Cherry Chimney
    Clover Cobalt Coconut Comet Compass Cookie Copper Cotton Crayon Cricket Cupcake Daisy Desert Dolphin Donkey
    Dragon Engine Falcon Feather Fern Ferret Fiddle Forest Fossil Gadget Garden Garlic Gecko Ginger Glacier
    Goose Granite Gravel Guitar Hammer Harbor Hazel Hedgehog Helmet Heron Hornet Iceberg Igloo Iris Jacket
    Jaguar Kayak Kettle Kitten Kiwi Koala Ladder Lantern Lemon Lettuce Lizard Llama Lobster Lotus Magnet
    Mammoth Mango Maple Marble Marlin Meadow Melon Meteor Mitten Monkey Moose Muffin Mushroom Narwhal Nectar
    Noodle Nutmeg Ocean Olive Onion Orchid Otter Oyster Paddle Panda Papaya Parrot Peach Peanut Pebble Pelican
    Pepper Pickle Pigeon Pillow Pirate Planet Plum Pocket Potato Pretzel Puffin Pumpkin Puzzle Quartz Quill
    Rabbit Radish Raven River Robin Rocket Saddle Salmon Sandal Sardine Saturn Scooter Shovel Shrimp Spider
    Spinach Sponge Sparrow Squash Squid Squirrel Statue Teapot Thimble Thistle Tiger Timber Toaster Tortoise
    Trumpet Tulip Turnip Turtle Velvet Violin Waffle Walnut Walrus Weasel Whistle Willow Wizard Yogurt Zebra
'''.split()

ADVERBS = '''
    Boldly Bravely Briefly Brightly Briskly Busily Calmly Carefully Casually Cheerfully Cleverly Cozily Crisply
    Curiously Daily Dearly Deftly Dimly Dutifully Eagerly Evenly Faintly Fairly Fiercely Firmly Fondly Freely
    Gently Gladly Gleefully Gracefully Grandly Gravely Handily Happily Hastily Heartily Honestly Humbly
    Jauntily Jokingly Jovially Joyfully Justly Keenly Kindly Lazily Lightly Loosely Lovingly Loyally Meekly
    Merrily Mightily Mildly Neatly Nicely Nimbly Noisily Oddly Openly Patiently Perfectly Plainly Playfully
    Politely Promptly Proudly Punctually Quaintly Quickly Quietly Quirkily Rapidly Readily Richly Rightly
    Roughly Safely Sharply Shyly Silently Simply Sincerely Sleepily Slowly Smoothly Snappily Snugly Softly
    Solemnly Speedily Spryly Steadily Sternly Stoutly Sturdily Suddenly Surely Sweetly Swiftly Tenderly
    Tidily Tightly Truly Usefully Valiantly Vastly Vividly Warily Warmly Wearily Wildly Wisely Wondrously
    Wryly Yearly Zealously Zestfully
'''.split()

_random = secrets.SystemRandom()

# One "key: value" line. Block scalar text and list items don't match and are skipped.
LINE = re.compile(r'''^(?P<indent>[ ]*)(?P<key>"[^"]*"|'[^']*'|[^\s#:'"-][^:#]*?)[ ]*:'''
                  r'''(?P<value>(?:[ ]+(?!\#)[^#\r\n]*?)?)(?P<rest>(?:[ ]+\#[^\r\n]*)?[ ]*)(?P<eol>\r?\n?)$''')


def soulseek_username():
    while True:
        first, second = _random.sample(NOUNS, 2)
        name = f'{first}{second}{_random.choice(ADVERBS)}{_random.randint(1000, 9999)}'
        if len(name) <= MAX_USERNAME:
            return name


def password():
    return ''.join(secrets.choice(PASSWORD_CHARS) for _ in range(PASSWORD_LENGTH))


def _placeholder(value, placeholders):
    return value is None or str(value).strip().lower() in placeholders


def _wanted(data):
    """{path of keys: new value} for every example credential still in the parsed config."""
    wanted = {}
    slsk = data.get('soulseek')
    if isinstance(slsk, dict):
        if 'username' in slsk and _placeholder(slsk['username'], PLACEHOLDER_USERNAMES):
            wanted[('soulseek', 'username')] = soulseek_username()
        if 'password' in slsk and _placeholder(slsk['password'], PLACEHOLDER_PASSWORDS):
            wanted[('soulseek', 'password')] = password()
    web = data.get('webserver')
    admins = web.get('admins') if isinstance(web, dict) else None
    if isinstance(admins, dict):
        for name, value in admins.items():
            if _placeholder(value, PLACEHOLDER_PASSWORDS):
                wanted[('webserver', 'admins', str(name))] = password()
    return wanted


def _get(data, keys):
    for key in keys:
        if not isinstance(data, dict):
            return None
        data = next((v for k, v in data.items() if str(k) == key), None)
    return data


def _replace(lines, wanted):
    """Puts each wanted value on its key's line, keeping indents, comments and line endings."""
    stack, done = [], set()
    for i, line in enumerate(lines):
        m = LINE.match(line)
        if not m:
            continue
        indent, key = len(m['indent']), m['key'].strip()
        if key[:1] in '"\'':
            key = key[1:-1]
        while stack and stack[-1][0] >= indent:
            stack.pop()
        stack.append((indent, key))
        path = tuple(k for _, k in stack)
        if path in wanted and path not in done:
            value = wanted[path] if path[-1] == 'username' else f"'{wanted[path]}'"
            lines[i] = f'{line[:m.end("key")]}: {value}{m["rest"]}{m["eol"]}'
            done.add(path)
    return done


def fill_defaults(path):
    """Replaces the example credentials left in config.yaml. Returns what was changed, for the log.

    A config.yaml that is missing or isn't valid YAML is left for config.load() to report.
    """
    path = Path(path)
    try:
        text = path.read_bytes().decode('utf-8')
        data = yaml.safe_load(text)
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return []
    wanted = _wanted(data) if isinstance(data, dict) else {}
    if not wanted:
        return []

    lines = text.splitlines(keepends=True)
    done = _replace(lines, wanted)
    new_text = ''.join(lines)
    try:
        new_data = yaml.safe_load(new_text)
    except yaml.YAMLError:
        new_data = None
    if done != set(wanted) or not isinstance(new_data, dict) \
            or any(_get(new_data, keys) != value for keys, value in wanted.items()):
        raise ConfigError(f'{path} still has the example Soulseek or admin credentials, and random ones '
                          'couldn\'t be filled in automatically. Set them in config.yaml yourself.')
    try:
        path.write_bytes(new_text.encode('utf-8'))   # in place: with Docker it's a mounted file
    except OSError as e:
        raise ConfigError(f'{path} still has the example credentials, and random ones couldn\'t be saved '
                          f'there ({e}). Set them in config.yaml yourself, or let Song-DL write to it.')

    changed = []
    for keys, value in wanted.items():
        if keys == ('soulseek', 'username'):
            changed.append(f'Soulseek username {value}')
        elif keys == ('soulseek', 'password'):
            changed.append('Soulseek password')
        else:
            changed.append(f'password for admin "{keys[-1]}"')
    return changed
