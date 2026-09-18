$ErrorActionPreference = "SilentlyContinue"
$workbenchUrl = "http://127.0.0.1:6866"
$healthUrl = "$workbenchUrl/api/health"

for ($attempt = 0; $attempt -lt 120; $attempt++) {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri $healthUrl -TimeoutSec 1
        if ($response.StatusCode -eq 200) {
            Start-Process $workbenchUrl
            exit 0
        }
    }
    catch {
        Start-Sleep -Milliseconds 500
    }
}

exit 1
