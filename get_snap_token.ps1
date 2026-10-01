$ErrorActionPreference = "Stop"

$defaultRedirectUri = "https://example.com/"
$scope = "snapchat-marketing-api"

function Read-Required([string]$Prompt) {
    do {
        $value = Read-Host $Prompt
    } while ([string]::IsNullOrWhiteSpace($value))
    return $value.Trim()
}

function Get-QueryValue([string]$Url, [string]$Name) {
    $uri = [Uri]$Url
    foreach ($part in $uri.Query.TrimStart("?").Split("&")) {
        if ([string]::IsNullOrWhiteSpace($part)) { continue }
        $pair = $part.Split("=", 2)
        if ($pair.Count -eq 2 -and $pair[0] -eq $Name) {
            return [Uri]::UnescapeDataString($pair[1].Replace("+", " "))
        }
    }
    return $null
}

Write-Host "Snapchat OAuth helper - second Ad Account bot" -ForegroundColor Cyan
Write-Host "Use an OAuth app created in an organization you control."
Write-Host "When the browser opens, sign in as the Snapchat user that can edit the target Ad Account."
Write-Host "The partner organization does not need to share any OAuth keys."
Write-Host "This helper does not save your client secret or tokens to a file."
Write-Host ""
$redirectInput = Read-Host "Redirect URI [$defaultRedirectUri]"
$redirectUri = if ([string]::IsNullOrWhiteSpace($redirectInput)) {
    $defaultRedirectUri
} else {
    $redirectInput.Trim()
}
Write-Host "The OAuth app Redirect URI must be exactly: $redirectUri" -ForegroundColor Yellow
Write-Host ""

$clientId = Read-Required "Paste SNAP_CLIENT_ID"
$secureSecret = Read-Host "Paste SNAP_CLIENT_SECRET" -AsSecureString
$clientSecret = [System.Net.NetworkCredential]::new("", $secureSecret).Password
if ([string]::IsNullOrWhiteSpace($clientSecret)) {
    throw "Client secret cannot be empty."
}

$state = [Guid]::NewGuid().ToString("N")
$authUrl = "https://accounts.snapchat.com/login/oauth2/authorize" +
    "?client_id=$([Uri]::EscapeDataString($clientId))" +
    "&redirect_uri=$([Uri]::EscapeDataString($redirectUri))" +
    "&response_type=code" +
    "&scope=$([Uri]::EscapeDataString($scope))" +
    "&state=$state"

Write-Host ""
Write-Host "A Snapchat authorization page will open." -ForegroundColor Cyan
Write-Host "IMPORTANT: authorize the Snapchat user that already has access to the second Ad Account." -ForegroundColor Yellow
Write-Host "Approve access, then copy the COMPLETE redirected URL from the browser address bar."
Write-Host "The URL must still contain both code= and state=."
Start-Process $authUrl

$returnedUrl = Read-Required "Paste the complete redirected URL"
$returnedState = Get-QueryValue $returnedUrl "state"
$code = Get-QueryValue $returnedUrl "code"

if ($returnedState -ne $state) {
    throw "OAuth state did not match. Stop and run this helper again."
}
if ([string]::IsNullOrWhiteSpace($code)) {
    throw "No OAuth code was found in the pasted URL."
}

$tokenRequest = @{
    Method = "Post"
    Uri = "https://accounts.snapchat.com/login/oauth2/access_token"
    ContentType = "application/x-www-form-urlencoded"
    Body = @{
        grant_type = "authorization_code"
        client_id = $clientId
        client_secret = $clientSecret
        code = $code
        redirect_uri = $redirectUri
    }
}
$tokenResponse = Invoke-RestMethod @tokenRequest

if ([string]::IsNullOrWhiteSpace($tokenResponse.refresh_token)) {
    throw "Snapchat did not return a refresh token."
}

try {
    Set-Clipboard -Value $tokenResponse.refresh_token
}
catch {
    throw "The refresh token was created but could not be copied to the Windows clipboard. It was not displayed or saved. Run this helper again in Windows PowerShell."
}

Write-Host ""
Write-Host "SUCCESS - SNAP_REFRESH_TOKEN was copied to your Windows clipboard." -ForegroundColor Green
Write-Host "Paste it directly into the GitHub Secret. Do not paste it into chat or a screenshot." -ForegroundColor Yellow
Write-Host ""
$null = Read-Host "After saving it as the GitHub Secret SNAP_REFRESH_TOKEN, press Enter to continue"

$headers = @{ Authorization = "Bearer $($tokenResponse.access_token)" }
$organizationRequest = @{
    Method = "Get"
    Uri = "https://adsapi.snapchat.com/v1/me/organizations?with_ad_accounts=true"
    Headers = $headers
}
$organizations = Invoke-RestMethod @organizationRequest

Write-Host "Organizations and ad accounts visible to this Snapchat user:" -ForegroundColor Cyan
$organizations.organizations | ForEach-Object {
    $organization = if ($_.organization) { $_.organization } else { $_ }
    Write-Host "Organization: $($organization.name) | ID: $($organization.id)"
    $organization.ad_accounts | ForEach-Object {
        $account = if ($_.ad_account) { $_.ad_account } else { $_ }
        Write-Host "  Ad Account: $($account.name) | ID: $($account.id)"
    }
}

Write-Host ""
Write-Host "The second Ad Account must appear above. If it does not, stop: accept its invitation, confirm the user role, and repeat authentication." -ForegroundColor Yellow

Write-Host ""
$adAccountId = Read-Required "Paste the exact COD Partner Ad Account ID"
$campaignUri = "https://adsapi.snapchat.com/v1/adaccounts/{0}/campaigns?limit=1000&sort=updated_at-desc" -f $adAccountId
$campaignRequest = @{
    Method = "Get"
    Uri = $campaignUri
    Headers = $headers
}
$campaignResponse = Invoke-RestMethod @campaignRequest

$campaigns = @()
$campaignResponse.campaigns | ForEach-Object {
    $campaign = if ($_.campaign) { $_.campaign } else { $_ }
    if ($campaign.id) {
        $campaigns += $campaign
    }
}

if ($campaigns.Count -eq 0) {
    throw "No campaigns were returned for this Ad Account. Confirm the ID and your user role."
}

Write-Host ""
Write-Host "Loading Ad Squads from all $($campaigns.Count) campaign(s)..." -ForegroundColor Cyan
$rows = @()
$rowNumber = 1

foreach ($campaign in $campaigns) {
    $squadUri = "https://adsapi.snapchat.com/v1/campaigns/{0}/adsquads?limit=1000&sort=updated_at-desc" -f $campaign.id
    $squadRequest = @{
        Method = "Get"
        Uri = $squadUri
        Headers = $headers
    }
    try {
        $squadResponse = Invoke-RestMethod @squadRequest
    }
    catch {
        Write-Warning "Could not load Ad Squads for campaign '$($campaign.name)': $($_.Exception.Message)"
        continue
    }

    $squadResponse.adsquads | ForEach-Object {
        $squad = if ($_.adsquad) { $_.adsquad } else { $_ }
        if ($squad.id) {
            $rows += [PSCustomObject]@{
                Number = $rowNumber
                CampaignName = [string]$campaign.name
                AdSquadName = [string]$squad.name
                AdSquadStatus = [string]$squad.status
                AdSquadId = [string]$squad.id
            }
            $rowNumber++
        }
    }
}

if ($rows.Count -eq 0) {
    throw "No Ad Squads were returned for this Ad Account."
}

Write-Host ""
Write-Host "Available Ad Squads:" -ForegroundColor Cyan
foreach ($row in $rows) {
    Write-Host "[$($row.Number)] Campaign: $($row.CampaignName) | Ad Squad: $($row.AdSquadName) | Status: $($row.AdSquadStatus)"
    Write-Host "    ID: $($row.AdSquadId)"
}

Write-Host ""
$selection = Read-Required "Enter Ad Squad numbers separated by commas (example: 1,2,5)"
$tokens = $selection -split '[,;\s]+' | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
$selectedRows = @()

foreach ($token in $tokens) {
    $number = 0
    if (-not [int]::TryParse($token, [ref]$number)) {
        throw "'$token' is not a valid list number. Enter numbers such as 1,2,5."
    }
    $match = $rows | Where-Object { $_.Number -eq $number } | Select-Object -First 1
    if (-not $match) {
        throw "Selection $number does not exist in the displayed list."
    }
    if ($selectedRows.AdSquadId -notcontains $match.AdSquadId) {
        $selectedRows += $match
    }
}

if ($selectedRows.Count -eq 0) {
    throw "Select at least one Ad Squad."
}
if ($selectedRows.Count -gt 20) {
    throw "This bot accepts a maximum of 20 Ad Squads in one target."
}

$selectedIds = ($selectedRows | ForEach-Object { $_.AdSquadId }) -join ","

Write-Host ""
Write-Host "Selected Ad Squads:" -ForegroundColor Green
foreach ($row in $selectedRows) {
    Write-Host "  $($row.AdSquadName) | $($row.AdSquadId)"
}

Write-Host ""
Write-Host "Create these exact GitHub Secrets:" -ForegroundColor Green
Write-Host "SNAP_AD_ACCOUNT_ID=$adAccountId"
Write-Host "SNAP_TARGET_1=$selectedIds"
Write-Host ""
Write-Host "The refresh token is already saved in GitHub. Keep these target values private." -ForegroundColor Yellow
