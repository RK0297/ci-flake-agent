import random
import sys
import time

def run_suite():
    print("=== Flaky Test Suite Execution ===")
    print("Running test_payment_service.py ...")
    
    # 40% probability of failure as requested
    if random.random() < 0.4:
        failure_type = random.choice(["timeout", "network", "race_condition", "oom"])
        
        if failure_type == "timeout":
            print("[INFO] Executing test_lock_acquisition...")
            time.sleep(1.0)
            print("[ERROR] TimeoutError: Lock acquisition timed out after 30.0s waiting for Redis lock 'payment_mutex'")
            print("Traceback (most recent call last):\n  File \"tests/test_payment.py\", line 54, in test_lock_acquisition\n    raise TimeoutError('Lock acquisition timed out')")
            print("[FAIL] test_lock_acquisition FAILED")
            sys.exit(1)
            
        elif failure_type == "network":
            print("[INFO] Executing test_gateway_handshake...")
            print("[ERROR] ConnectionError: HTTPSConnectionPool(host='api.payments.internal', port=443): Max retries exceeded with url: /v1/charge (Caused by NewConnectionError('<urllib3.connection.HTTPSConnection object>: Failed to establish a new connection: [Errno 111] Connection refused'))")
            print("Traceback (most recent call last):\n  File \"tests/test_payment.py\", line 88, in test_gateway_handshake\n    raise ConnectionError('Failed to connect to payment endpoint')")
            print("[FAIL] test_gateway_handshake FAILED")
            sys.exit(1)
            
        elif failure_type == "race_condition":
            print("[INFO] Executing test_concurrent_balance_update...")
            val = random.randint(90, 99)
            print(f"[ERROR] AssertionError: Race condition detected in shared state. Expected balance 100, found {val}")
            print(f"Traceback (most recent call last):\n  File \"tests/test_payment.py\", line 112, in test_concurrent_balance_update\n    assert balance == 100, f'Expected 100, got {val}'")
            print("[FAIL] test_concurrent_balance_update FAILED")
            sys.exit(1)
            
        elif failure_type == "oom":
            print("[INFO] Executing test_large_batch_export...")
            print("Fatal Python error: Out of memory (OOM)")
            print("[ERROR] MemoryError: Unable to allocate 4.12 GiB for array with shape (50000, 50000) and data type float64")
            print("Process terminated by SIGKILL (Exit code 137 / Out Of Memory)")
            sys.exit(1)
    else:
        print("[PASS] test_lock_acquisition PASSED")
        print("[PASS] test_gateway_handshake PASSED")
        print("[PASS] test_concurrent_balance_update PASSED")
        print("[PASS] test_large_batch_export PASSED")
        print("=== 4 passed in 0.42s ===")
        sys.exit(0)

if __name__ == "__main__":
    run_suite()
