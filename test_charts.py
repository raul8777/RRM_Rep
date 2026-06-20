#!/usr/bin/env python3
"""
Test script to verify price history endpoint and chart functionality
"""
import requests
import json
from datetime import datetime, timedelta

def test_price_history():
    """Test the price history endpoint with various symbols"""
    base_url = "http://localhost:8000"
    
    # Test symbols
    symbols = ["BTCUSDT", "ETHUSDT", "XRPUSDT"]
    
    for symbol in symbols:
        print(f"\n=== Testing {symbol} ===")
        
        # Get recent data (last 6 hours)
        end_time = int(datetime.now().timestamp() * 1000)
        start_time = end_time - (6 * 60 * 60 * 1000)  # 6 hours ago
        
        url = f"{base_url}/price-history/{symbol}?start_time={start_time}&end_time={end_time}"
        
        try:
            response = requests.get(url)
            print(f"Status: {response.status_code}")
            
            if response.status_code == 200:
                data = response.json()
                
                if 'error' in data and data['error']:
                    print(f"❌ Error: {data['error']}")
                    continue
                
                if 'data' in data:
                    points = len(data['data'])
                    print(f"✅ Data points: {points}")
                    
                    if points > 0:
                        first = data['data'][0]
                        last = data['data'][-1]
                        
                        print(f"   Time range: {datetime.fromtimestamp(first['timestamp']/1000)} to {datetime.fromtimestamp(last['timestamp']/1000)}")
                        print(f"   Price range: ${first['close']:.2f} → ${last['close']:.2f}")
                        print(f"   Sample keys: {list(first.keys())}")
                    else:
                        print("⚠️  No data points returned")
                else:
                    print("❌ No 'data' key in response")
            else:
                print(f"❌ HTTP Error: {response.text}")
                
        except Exception as e:
            print(f"❌ Exception: {e}")

def test_dashboard_data():
    """Test if dashboard has closed positions to test charts with"""
    base_url = "http://localhost:8000"
    
    print("\n=== Testing Dashboard Data ===")
    
    # Test closed positions
    try:
        response = requests.get(f"{base_url}/positions/closed")
        print(f"Closed positions status: {response.status_code}")
        
        if response.status_code == 200:
            data = response.json()
            positions = data.get('positions', [])
            print(f"Closed positions: {len(positions)}")
            
            if positions:
                pos = positions[0]
                print(f"Sample position: {pos.get('symbol')} - Entry: ${pos.get('entry_price')} → Exit: ${pos.get('exit_price')}")
                
                if pos.get('entry_time') and pos.get('exit_time'):
                    print(f"Time range: {pos.get('entry_time')} → {pos.get('exit_time')}")
                else:
                    print("⚠️  Missing timestamps for time series chart")
            else:
                print("⚠️  No closed positions available for testing")
        else:
            print(f"❌ Error: {response.text}")
            
    except Exception as e:
        print(f"❌ Exception: {e}")
    
    # Test futures closed
    try:
        response = requests.get(f"{base_url}/futures/closed")
        print(f"Futures closed status: {response.status_code}")
        
        if response.status_code == 200:
            data = response.json()
            positions = data.get('positions', [])
            print(f"Futures closed: {len(positions)}")
            
            if positions:
                pos = positions[0]
                print(f"Sample future: {pos.get('symbol')} {pos.get('side')} - Entry: ${pos.get('entry_price')} → Exit: ${pos.get('exit_price')}")
                
                if pos.get('opened_at') and pos.get('closed_at'):
                    print(f"Time range: {pos.get('opened_at')} → {pos.get('closed_at')}")
                else:
                    print("⚠️  Missing timestamps for time series chart")
            else:
                print("⚠️  No futures closed available for testing")
        else:
            print(f"❌ Error: {response.text}")
            
    except Exception as e:
        print(f"❌ Exception: {e}")

if __name__ == "__main__":
    print("🔍 Testing Price History Endpoint and Chart Data")
    print("=" * 50)
    
    test_price_history()
    test_dashboard_data()
    
    print("\n" + "=" * 50)
    print("✅ Test completed!")
