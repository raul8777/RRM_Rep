param(
    [string]$Symbols = "ENAUSDT,ADAUSDT,BTCUSDT",
    [string]$CloseReason = "manual_close_small_runner",
    [double]$CommissionRate = 0.001
)

docker exec crypto-agent-app python /app/Dev/tools/spot_close/close_if_non_negative.py --symbols $Symbols --close-reason $CloseReason --commission-rate $CommissionRate
