$ErrorActionPreference = "Stop"

function Read-Required([string]$Prompt) {
    do {
        $value = Read-Host $Prompt
    } while ([string]::IsNullOrWhiteSpace($value))
    return $value.Trim()
}

function Read-RequiredSecret([string]$Prompt) {
    $secureValue = Read-Host $Prompt -AsSecureString
    $plainValue = [System.Net.NetworkCredential]::new("", $secureValue).Password
    if ([string]::IsNullOrWhiteSpace($plainValue)) {
        throw "$Prompt cannot be empty."
    }
    return $plainValue
}

Write-Host "Snapchat multi-squad selector" -ForegroundColor Cyan
Write-Host "This helper does not save or display your Client Secret or Refresh Token."
Write-Host "Enter credentials only in this PowerShell window."
Write-Host ""

$clientId = Read-Required "Paste SNAP_CLIENT_ID"
$clientSecret = Read-RequiredSecret "Paste SNAP_CLIENT_SECRET"
$refreshToken = Read-RequiredSecret "Paste SNAP_REFRESH_TOKEN"

$tokenRequest = @{
    Method = "Post"
    Uri = "https://accounts.snapchat.com/login/oauth2/access_token"
    ContentType = "application/x-www-form-urlencoded"
    Body = @{
        grant_type = "refresh_token"
        client_id = $clientId
        client_secret = $clientSecret
        refresh_token = $refreshToken
    }
}

Write-Host ""
Write-Host "Checking the credentials..." -ForegroundColor Cyan
$tokenResponse = Invoke-RestMethod @tokenRequest
if ([string]::IsNullOrWhiteSpace($tokenResponse.access_token)) {
    throw "Snapchat did not return an access token. Confirm that all three credentials belong to the same OAuth app."
}
Write-Host "Credentials accepted." -ForegroundColor Green

$headers = @{ Authorization = "Bearer $($tokenResponse.access_token)" }
$organizationRequest = @{
    Method = "Get"
    Uri = "https://adsapi.snapchat.com/v1/me/organizations?with_ad_accounts=true"
    Headers = $headers
}
$organizations = Invoke-RestMethod @organizationRequest

Write-Host ""
Write-Host "Organizations and Ad Accounts:" -ForegroundColor Cyan
$organizations.organizations | ForEach-Object {
    $organization = if ($_.organization) { $_.organization } else { $_ }
    Write-Host "Organization: $($organization.name) | ID: $($organization.id)"
    $organization.ad_accounts | ForEach-Object {
        $account = if ($_.ad_account) { $_.ad_account } else { $_ }
        Write-Host "  Ad Account: $($account.name) | ID: $($account.id)"
    }
}

Write-Host ""
$adAccountId = Read-Required "Paste the exact Ad Account ID"
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
    throw "No campaigns were returned for this Ad Account."
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
                CampaignId = [string]$campaign.id
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
$selection = Read-Required "Enter Ad Squad numbers separated by commas (example: 1,2,5,7)"
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
    throw "This bot accepts a maximum of 20 Ad Squads in one workflow."
}

$selectedIds = ($selectedRows | ForEach-Object { $_.AdSquadId }) -join ","

Write-Host ""
Write-Host "Selected Ad Squads:" -ForegroundColor Green
foreach ($row in $selectedRows) {
    Write-Host "  $($row.AdSquadName) | $($row.AdSquadId)"
}

Write-Host ""
Write-Host "Add this GitHub Secret:" -ForegroundColor Green
Write-Host "SNAP_AD_ACCOUNT_ID=$adAccountId"
Write-Host ""
Write-Host "Add the selected UUID list to a private target Secret:" -ForegroundColor Green
Write-Host "SNAP_TARGET_1=$selectedIds"
Write-Host ""
Write-Host "Next: select target_1 in the workflow and run test mode before live mode."
