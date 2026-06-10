# Herder launchd daemon (macOS)

## Install
1. Ensure the log directory exists:
   ```sh
   mkdir -p "$PWD/.herder"    # launchd needs the log dir to exist BEFORE load
   ```
2. Copy the plist, replacing placeholders with YOUR paths:
   - `__HERDER_DIR__`  -> absolute path of this Herder checkout
   - `__HERDER_HOME__` -> your state dir (default: `<HERDER_DIR>/.herder`)
   ```sh
   sed -e "s|__HERDER_DIR__|$PWD|g" -e "s|__HERDER_HOME__|$PWD/.herder|g" \
       launchd/ai.herder.worker.plist > ~/Library/LaunchAgents/ai.herder.worker.plist
   ```
3. Load it:
   ```sh
   launchctl load ~/Library/LaunchAgents/ai.herder.worker.plist
   ```
4. Verify:
   ```sh
   launchctl list | grep herder          # running
   uv run herder --config config.yaml doctor --min-ok 3   # CLIs authenticated under daemon env
   tail -f .herder/worker.log
   ```

## Uninstall
```sh
launchctl unload ~/Library/LaunchAgents/ai.herder.worker.plist
rm ~/Library/LaunchAgents/ai.herder.worker.plist
```

## Notes
- `KeepAlive=true`: relaunches on crash; survives logout/reboot (RunAtLoad).
- A worker killed mid-job leaves the job leased; the lease expires
  (`worker.lease_seconds`) and the next pass reclaims it. Long jobs renew
  their lease via heartbeat while running.
- Secrets: export API keys (e.g. COMMAND_CODE_API_KEY) in `~/.zshrc` /
  `~/.zprofile` -- the wrapper sources them. Never commit keys.
