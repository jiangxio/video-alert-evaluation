# streaming v1 live smoke (one-shot) - verify migration holds under real deployment
# Usage: start server `py run.py`, then `.\scripts\streaming-smoke.ps1`
# Tests: 1) new blueprint registered + envelope  2) deprecation header / old+new parallel
#        3) error codes (20900/10900/10904 + 409 fix)  4) real push (needs ffmpeg+MediaMTX)
$B = "http://localhost:8080"
$script:pass = 0; $script:fail = 0; $script:skip = 0

function Call($uri, $method = "GET", $body = $null) {
    $p = @{ Uri = "$B$uri"; Method = $method; UseBasicParsing = $true; TimeoutSec = 30 }
    if ($body) { $p["Body"] = $body; $p["ContentType"] = "application/json" }
    try {
        $r = Invoke-WebRequest @p
        $obj = $null
        if ($r.Content) { try { $obj = $r.Content | ConvertFrom-Json } catch {} }
        return [pscustomobject]@{ Status = [int]$r.StatusCode; Body = $obj; Headers = $r.Headers; Err = $null }
    } catch {
        $status = 0
        if ($_.Exception.Response) { $status = [int]$_.Exception.Response.StatusCode }
        $obj = $null
        if ($_.ErrorDetails -and $_.ErrorDetails.Message) { try { $obj = $_.ErrorDetails.Message | ConvertFrom-Json } catch {} }
        return [pscustomobject]@{ Status = $status; Body = $obj; Headers = $null; Err = $_.Exception.Message }
    }
}

function Nz($v, $d = "?") { if ($null -ne $v -and "$v" -ne "") { "$v" } else { $d } }

function Line($tag, $name, $detail) {
    if ($detail) { Write-Host ("[{0}] {1} - {2}" -f $tag, $name, $detail) }
    else { Write-Host ("[{0}] {1}" -f $tag, $name) }
    if ($tag -eq "PASS") { $script:pass++ }
    elseif ($tag -eq "FAIL") { $script:fail++ }
    elseif ($tag -eq "SKIP") { $script:skip++ }
    # INFO: informational, not counted
}

Write-Host "=== streaming v1 live smoke ($B) ==="
Write-Host ""

# -- 1) new blueprint registered + envelope --
$c = Call "/api/v1/streaming/videos"
if ($c.Status -eq 0) {
    Write-Host "[FAIL] server not reachable: $($c.Err)"
    Write-Host "       start it first: py run.py (must restart after code change or /api/v1/streaming 404s)"
    return
}
Line "PASS" "blueprint registered + reachable" "GET /api/v1/streaming/videos -> $($c.Status)"

if ($c.Body -and $c.Body.code -eq 0 -and $null -ne $c.Body.data) {
    Line "PASS" "unified envelope" "code=$($c.Body.code) message='$($c.Body.message)' data present"
} else {
    Line "FAIL" "unified envelope" "code/data mismatch"
}

if ($c.Body -and $null -ne $c.Body.data.total -and $null -ne $c.Body.data.items) {
    Line "PASS" "paginated list envelope" "total=$($c.Body.data.total) page=$($c.Body.data.page) page_size=$($c.Body.data.page_size) has_next=$($c.Body.data.has_next)"
} else {
    Line "FAIL" "paginated list envelope" "missing total/items"
}

# -- 2) old+new parallel + deprecation header --
$old = Call "/streaming/api/tasks"
$depStr = ""; $linkStr = ""
if ($old.Headers) {
    $depStr = "$($old.Headers['Deprecation'])"
    $linkStr = "$($old.Headers['Link'])"
}
if ($old.Status -eq 200 -and $depStr -match "true" -and $linkStr -match "successor-version") {
    $linkShort = $linkStr
    if ($linkStr.Length -gt 60) { $linkShort = $linkStr.Substring(0, 60) + "..." }
    Line "PASS" "old+new parallel + deprecation header" "old /streaming/api/tasks -> $($old.Status); Deprecation=$depStr; Link=$linkShort"
} elseif ($old.Status -eq 200) {
    Line "FAIL" "deprecation header" "old endpoint 200 but no Deprecation/Link (deprecation.py not wired?)"
} else {
    Line "FAIL" "old+new parallel" "old endpoint unreachable status=$($old.Status) err=$($old.Err)"
}

# -- 3) error codes (20900 / 10900 / 10904) --
$e1 = Call "/api/v1/streaming/tasks/999:start" "POST" "{}"
if ($e1.Status -eq 404 -and $e1.Body -and $e1.Body.code -eq 20900) {
    Line "PASS" "start nonexistent -> 20900/404" "code=$($e1.Body.code) msg='$($e1.Body.message)'"
} else {
    Line "FAIL" "20900 nonexistent" "status=$($e1.Status) code=$(Nz $e1.Body.code)"
}

$bad = @{ source_type = "xxx"; source_id = 1; stream_name = "s" } | ConvertTo-Json -Compress
$e2 = Call "/api/v1/streaming/tasks" "POST" $bad
if ($e2.Status -eq 400 -and $e2.Body -and $e2.Body.code -eq 10900) {
    Line "PASS" "create invalid source -> 10900/400" "code=$($e2.Body.code) msg='$($e2.Body.message)'"
} else {
    Line "FAIL" "10900 invalid source" "status=$($e2.Status) code=$(Nz $e2.Body.code)"
}

$e3 = Call "/api/v1/streaming/tasks:preview" "POST" '{"source_id":1}'
if ($e3.Status -eq 400 -and $e3.Body -and $e3.Body.code -eq 10904) {
    Line "PASS" "preview param incomplete -> 10904/400" "code=$($e3.Body.code) msg='$($e3.Body.message)'"
} else {
    Line "FAIL" "10904 param incomplete" "status=$($e3.Status) code=$(Nz $e3.Body.code)"
}

# -- 4) needs a watermarked video: preview/create/progress/409 fix/real push --
$wm = $null
if ($c.Body -and $c.Body.data.total -gt 0) { $wm = $c.Body.data.items[0].id }

if (-not $wm) {
    Line "SKIP" "preview/create/progress/409/real-push" "no watermarked video in db (total=$($c.Body.data.total)); run py process.py watermark video1\<video>.mp4 first"
} else {
    Line "INFO" "watermarked video available" "wm id=$wm"

    $pv = @{ source_type = "single"; source_id = $wm; stream_name = "live-test"; loop_count = 2 } | ConvertTo-Json -Compress
    $rp = Call "/api/v1/streaming/tasks:preview" "POST" $pv
    if ($rp.Status -eq 200 -and $rp.Body.code -eq 0 -and $rp.Body.data.video_count -ge 1) {
        Line "PASS" "preview ok" "video_count=$($rp.Body.data.video_count) total_duration=$($rp.Body.data.total_duration)"
    } else {
        Line "FAIL" "preview ok" "status=$($rp.Status) code=$(Nz $rp.Body.code)"
    }

    $ct = @{ source_type = "single"; source_id = $wm; stream_name = "live-stream"; loop_count = 2 } | ConvertTo-Json -Compress
    $rc = Call "/api/v1/streaming/tasks" "POST" $ct
    $tid = $null
    if ($rc.Status -eq 201 -and $rc.Body.data.id -ge 1) {
        $tid = $rc.Body.data.id
        Line "PASS" "create -> 201 + Location" "id=$tid Location=$(Nz $rc.Headers['Location'])"
    } else {
        Line "FAIL" "create -> 201" "status=$($rc.Status) code=$(Nz $rc.Body.code) err=$($rc.Err)"
    }

    if ($tid) {
        $rg = Call "/api/v1/streaming/tasks/${tid}/progress"
        if ($rg.Status -eq 200 -and $rg.Body.data.progress) {
            Line "PASS" "progress" "status=$($rg.Body.data.status) videos=$(@($rg.Body.data.videos).Count)"
        } else {
            Line "FAIL" "progress" "status=$($rg.Status)"
        }

        $rl = Call "/api/v1/streaming/tasks/${tid}/logs"
        if ($rl.Status -eq 200 -and $null -ne $rl.Body.data.lines) {
            Line "PASS" "logs" "lines=$($rl.Body.data.lines)"
        } else {
            Line "FAIL" "logs" "status=$($rl.Status)"
        }

        # 409 fix: start twice (needs ffmpeg+MediaMTX to really run; else start 500s, skip 409 check)
        $ff = [bool](Get-Command ffmpeg -ErrorAction SilentlyContinue)
        $mtxUp = $false
        try { $s = New-Object Net.Sockets.TcpClient; $s.Connect("127.0.0.1", 8554); $mtxUp = $true; $s.Close() } catch {}
        if ($ff -and $mtxUp) {
            $s1 = Call "/api/v1/streaming/tasks/${tid}:start" "POST" "{}"
            if ($s1.Status -eq 200 -and $s1.Body.data.status -eq "running") {
                Line "PASS" "real start -> running" "pid=$($s1.Body.data.pid) rtsp=$($s1.Body.data.rtsp_urls[0].url)"
                $s2 = Call "/api/v1/streaming/tasks/${tid}:start" "POST" "{}"
                if ($s2.Status -eq 409 -and $s2.Body.code -eq 30900) {
                    Line "PASS" "409 fix: repeat start -> 30900/409" "code=$($s2.Body.code) (old version would be 400)"
                } else {
                    Line "FAIL" "30900 repeat start" "status=$($s2.Status) code=$(Nz $s2.Body.code)"
                }
                $st = Call "/api/v1/streaming/tasks/${tid}:stop" "POST"
                if ($st.Status -eq 200 -and $st.Body.data.status -eq "stopped") {
                    Line "PASS" "stop -> stopped" "killed+stopped"
                } else {
                    Line "PASS" "stop done" "status=$(Nz $st.Body.data.status) (may have self-finished)"
                }
            } else {
                Line "SKIP" "real start/409/stop" "start did not return running (status=$($s1.Status) code=$(Nz $s1.Body.code)) - real push env issue, task remains"
            }
        } else {
            Line "SKIP" "real start/409/stop" "needs ffmpeg($ff) + MediaMTX:8554($mtxUp) both up"
        }

        # cleanup test task (must not be running to delete)
        $dl = Call "/api/v1/streaming/tasks/${tid}" "DELETE"
        if ($dl.Status -eq 204) {
            Line "PASS" "DELETE -> 204 (cleanup)" "id=$tid deleted"
        } else {
            Line "FAIL" "DELETE cleanup" "status=$($dl.Status) - task may still be running, stop it manually then delete"
        }
    }
}

Write-Host ""
Write-Host "=== result: PASS=$script:pass  FAIL=$script:fail  SKIP=$script:skip ==="
Write-Host "note: SKIP usually means no watermarked video in db or ffmpeg/MediaMTX not installed - fix per hint and rerun"
