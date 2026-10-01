# Use your OAuth app with a partner Ad Account

Your OAuth app identifies the software. The user who authorizes it determines the
account permissions. An app in your own organization can therefore use your user's
existing access to a COD Partner account; it does not grant extra permissions.
See [Snapchat authentication](https://developers.snap.com/marketing-api/Ads-API/authentication).

If your current Client ID, Client Secret and refresh token already work with the
target account, reuse them. Installing this package does not require new tokens.

If you need to authorize an app:

1. In an organization where you are Admin, open Business Details and create an
   OAuth app. Save its Client ID and Client Secret privately.
2. Configure its Redirect URI. The included helper defaults to
   `https://example.com/`; use the exact URI configured on your app.
3. In PowerShell inside the extracted folder, run:

   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File .\get_snap_token.ps1
   ```

4. Sign in as the Snapchat user that can edit the target partner account. Follow
   the helper's authorization instructions and paste the copied token directly
   into the GitHub `SNAP_REFRESH_TOKEN` Secret.
5. Select the correct account and squads, then save `SNAP_AD_ACCOUNT_ID` and
   `SNAP_TARGET_1` privately in GitHub.

The partner does not need to give you its OAuth keys. If an API request is denied,
check the authorized user's role on the exact target account.

Use one repository and separate saved state per Ad Account. For a new account,
generate a new state encryption key; do not copy another account's state.
