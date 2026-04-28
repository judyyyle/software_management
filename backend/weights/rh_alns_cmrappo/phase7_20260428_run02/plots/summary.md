# CMRAPPO Result Summary

## Final Training Step
- update: 367
- global_step: 1500000
- reward_mean: 5.842548
- return_mean: 87.775841
- value_loss: 0.314394
- entropy: 0.027856
- approx_kl: 0.004629

## Final Eval Checkpoint
- update: 367
- global_step: 1500000

## Benchmark
- mean_total_reward: -1602.074690
- mean_delivery_count: 4.000000
- sum_timeout_order_count: 6
- sum_fallback_count: 0
- mode_b_dispatch_ratio: 1.000000
- mode_c_dispatch_ratio: 0.000000

## Stochastic Profiles
### low
- mean_total_reward: -199.104568
- mean_delivery_count: 4.000000
- sum_timeout_order_count: 0
- sum_fallback_count: 6
- mode_b_dispatch_ratio: 0.714286
- mode_c_dispatch_ratio: 0.285714

### medium
- mean_total_reward: 373.462391
- mean_delivery_count: 16.000000
- sum_timeout_order_count: 1
- sum_fallback_count: 15
- mode_b_dispatch_ratio: 0.819277
- mode_c_dispatch_ratio: 0.180723

### high
- mean_total_reward: -490.718784
- mean_delivery_count: 20.000000
- sum_timeout_order_count: 9
- sum_fallback_count: 14
- mode_b_dispatch_ratio: 0.857143
- mode_c_dispatch_ratio: 0.142857
