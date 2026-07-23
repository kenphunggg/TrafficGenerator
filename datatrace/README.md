# Datatrace Insights

Generated from the files in this directory.

## Executive Summary

Both traces cover exactly **31 days**.

This is based on a minute range from `0` to `44639`, inclusive:

```text
44,640 minutes / 1,440 minutes per day = 31 days
```

Both files contain one continuous row per minute, no missing minute buckets, and no invalid rows. Each trace currently contains one function/application: `yolo_x_cpu`.

## Source Files

| File | Rows | App/function count | Minute range | Duration | Missing minutes | Bad rows |
| --- | ---: | ---: | --- | ---: | ---: | ---: |
| `day_night.csv` | 44,640 | 1 | `0`-`44639` | 31 days | 0 | 0 |
| `non_station.csv` | 44,640 | 1 | `0`-`44639` | 31 days | 0 | 0 |

## Overall Request Volume

| Trace | Total requests | Average RPM | Median RPM | P95 RPM | Peak minute | Peak RPM |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `day_night` | 29,313,681 | 656.67 | 747 | 1,306 | 21,302 | 3,825 |
| `non_station` | 24,090,964 | 539.67 | 326 | 1,886 | 38,131 | 2,910 |

## Main Insights

- `day_night` has the larger total volume: **29.31M** requests versus **24.09M** in `non_station`.
- `day_night` is about **21.7% higher** in total request volume than `non_station`.
- `day_night` has a higher average and median RPM, so it behaves more like a consistently busy workload.
- `non_station` has a much lower median RPM but a higher P95 RPM, which means it spends many minutes at lower traffic and then jumps into stronger bursts.
- `day_night` reaches the highest single-minute spike: **3,825 RPM** at minute `21302`.
- `non_station` reaches its highest spike later in the trace: **2,910 RPM** at minute `38131`.
- Both traces are clean for replay: there are no missing minute buckets and no malformed rows.

## Application Breakdown

| Trace | Function/app | Requests | Share | Active minutes |
| --- | --- | ---: | ---: | ---: |
| `day_night` | `yolo_x_cpu` | 29,313,681 | 100% | 44,640 |
| `non_station` | `yolo_x_cpu` | 24,090,964 | 100% | 44,640 |

## Daily Totals

| Day | `day_night` requests | `day_night` avg RPM | `non_station` requests | `non_station` avg RPM |
| ---: | ---: | ---: | ---: | ---: |
| 1 | 894,939 | 621.49 | 1,080,883 | 750.61 |
| 2 | 834,492 | 579.51 | 399,544 | 277.46 |
| 3 | 834,606 | 579.59 | 362,804 | 251.95 |
| 4 | 854,020 | 593.07 | 1,188,591 | 825.41 |
| 5 | 888,809 | 617.23 | 1,214,760 | 843.58 |
| 6 | 898,126 | 623.70 | 1,190,548 | 826.77 |
| 7 | 909,873 | 631.86 | 1,165,743 | 809.54 |
| 8 | 967,043 | 671.56 | 990,560 | 687.89 |
| 9 | 901,354 | 625.94 | 392,267 | 272.41 |
| 10 | 858,180 | 595.96 | 372,514 | 258.69 |
| 11 | 940,407 | 653.06 | 1,039,690 | 722.01 |
| 12 | 995,555 | 691.36 | 1,041,241 | 723.08 |
| 13 | 1,032,261 | 716.85 | 1,035,237 | 718.91 |
| 14 | 1,112,905 | 772.85 | 954,488 | 662.84 |
| 15 | 934,181 | 648.74 | 396,232 | 275.16 |
| 16 | 869,235 | 603.64 | 277,531 | 192.73 |
| 17 | 839,436 | 582.94 | 315,625 | 219.18 |
| 18 | 836,848 | 581.14 | 211,703 | 147.02 |
| 19 | 841,605 | 584.45 | 210,502 | 146.18 |
| 20 | 825,405 | 573.20 | 238,169 | 165.40 |
| 21 | 879,788 | 610.96 | 258,193 | 179.30 |
| 22 | 894,677 | 621.30 | 347,076 | 241.03 |
| 23 | 944,825 | 656.13 | 1,170,826 | 813.07 |
| 24 | 1,001,014 | 695.15 | 1,219,093 | 846.59 |
| 25 | 1,020,826 | 708.91 | 1,302,822 | 904.74 |
| 26 | 994,472 | 690.61 | 1,283,911 | 891.60 |
| 27 | 1,064,546 | 739.27 | 1,305,999 | 906.94 |
| 28 | 1,087,247 | 755.03 | 1,277,755 | 887.33 |
| 29 | 1,117,572 | 776.09 | 1,061,954 | 737.47 |
| 30 | 1,120,275 | 777.97 | 413,186 | 286.93 |
| 31 | 1,119,159 | 777.19 | 371,517 | 258.00 |

## Traffic Generator Implications

- A replay engine can use one scheduler tick per minute and convert each row with `requests_per_second = count / 60`.
- `day_night` is useful for testing a sustained workload with regular daily variation and a high baseline.
- `non_station` is better for testing abrupt traffic regime changes, autoscaling behavior, backlog handling, and recovery after bursts.
- Since both traces target only `yolo_x_cpu`, the first generator can use a single target endpoint or function mapping.
- The parser should still support multiple `function_id` values so future traces can add more applications without changing the replay model.

## Recommended First Replay Modes

| Mode | Purpose |
| --- | --- |
| Real-time replay | Send traffic using the original one-minute buckets. |
| Accelerated replay | Compress 31 days into a shorter test window using a speed multiplier. |
| Dry run | Print planned per-minute request counts without sending requests. |
| Daily slice replay | Replay selected days, for example the peak days or low-traffic days. |

## Notable Test Slices

| Trace | Slice | Why it is useful |
| --- | --- | --- |
| `day_night` | Days 29-31 | Sustained high-volume period around 1.12M requests/day. |
| `day_night` | Day 20 | Lowest-volume day in this trace. |
| `non_station` | Days 18-21 | Extended low-volume period. |
| `non_station` | Days 23-29 | Strong high-volume period with the trace maximum daily load. |
| `non_station` | Day 27 | Highest daily total: 1,305,999 requests. |
