# sonic-analysis

Analyze the audio of albums you are seeding on Orpheus (OPS) and contribute
"sonic fingerprints" so the tracker can suggest **acoustically similar
releases**. The tool scans your OPS `.torrent` files, runs
[Essentia](https://essentia.upf.edu/) analysis on the matching audio
(tempo, key, loudness, dynamic range, spectral shape, MFCCs), and uploads the
per-track data to OPS, which aggregates it into an album fingerprint.

Everything runs in Docker, so you don't need to install Essentia or Python
locally.

## What you need

- **Docker** (with the `docker compose` plugin).
- An **OPS API token** — create one under
  *Settings → Access Settings → API Keys* on OPS.
- To be **actively seeding** the torrents you want to analyze. The upload is
  rejected for any torrent you are not seeding — this ties each fingerprint to
  the specific torrent it came from, submitted by someone who actually has it.
  Simply having the `.torrent` file is not enough; your client must be seeding it.

## Quick start

```bash
cp .env.example .env        # then edit: token + your torrent directory
docker compose build        # one-time (installs Essentia; takes a few minutes)

# Analyze everything you are seeding and upload the results:
docker compose run --rm sonic-analysis --upload
```

The container uses fixed paths internally (`/torrents`, `/music`, `/output`); the
host directories they map to are set in `.env` (see below), so you don't pass
any paths on the command line. Results land in `./output/results.json` on the host.

## Configuration (`.env`)

| Variable        | Purpose                                                            |
|-----------------|-------------------------------------------------------------------|
| `OPS_API_TOKEN` | Your OPS API token (required for `--upload`).                      |
| `TORRENT_DIR`   | Host directory holding your OPS `.torrent` files → `/torrents`.    |
| `MUSIC_DIR`     | *Optional.* Host directory of downloaded music, if separate.      |
| `OUTPUT_DIR`    | Where the results JSON is written (default `./output`).           |
| `OPS_API_URL`   | Override the API endpoint (default `https://orpheus.network/ajax.php`). |

## Windows

Install **Docker Desktop** and make sure it's running — it's the engine, and
Essentia/Python run inside the Linux container, so there's nothing else to
install. You then run the `docker compose run …` commands in a **terminal**,
either **PowerShell** or a **WSL** shell.

The only Windows-specific detail is how you write the host paths in `.env`, which
depends on the terminal you use:

- From **PowerShell**, use forward slashes with the drive letter:
  `TORRENT_DIR=C:/Users/You/AppData/Local/qBittorrent/BT_backup`
- From **WSL**, use `/mnt/<drive>/…`:
  `TORRENT_DIR=/mnt/c/Users/You/.../BT_backup`

Don't use backslashes (`C:\Users\…`) — Docker won't parse the mount.

## Music stored separately from the .torrent files

Some clients (rtorrent, Deluge, …) keep `.torrent` session files in one place
and the downloaded data in another. In that case set `MUSIC_DIR` in `.env` and
add `--music-dir /music` to the command:

```bash
docker compose run --rm sonic-analysis --upload --music-dir /music
```

The tool matches each `.torrent` to its album folder by name — first next to
the `.torrent` file, then anywhere under `/music` (searched recursively, so an
`Artist/Album` layout works too).

## Upload-only mode

Analysis is slow (a few seconds per track). The results are cached in the JSON
file, so you can re-run the upload later without re-analyzing:

```bash
docker compose run --rm sonic-analysis --upload-only
```

## Output

`results.json` is keyed by torrent infohash and, for each release, stores the
per-track analysis (`tracks`) that gets uploaded, plus album metadata for local
use. Re-running updates the file in place (use `--overwrite` to start fresh).

## Running without Docker

The tool is a single script, `sonic_analysis.py`, needing Python 3.11+ with
`essentia numpy mutagen bencodepy requests`. Docker is strongly recommended —
Essentia can be awkward to install natively. Outside the container, pass your
`.torrent` directory and an output path explicitly (the `/torrents` and
`/output/results.json` defaults only exist inside Docker):

```bash
python sonic_analysis.py ~/torrents results.json --upload
```
