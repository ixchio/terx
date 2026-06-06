# TERX vs. Raw browser-use Benchmark Results

This benchmark runs 10 identical, multi-step browser tasks comparing a raw **browser-use** agent (modeled with real-world token sizes and GPT-4o latency) against **TERX** using dynamic CDP replaying.

| Task Name | Steps | Cold Time | Warm Time | Speedup | Cold Tokens | Warm Tokens | Cold Cost | Warm Cost | Savings |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| User Login Flow | 2 | 4.57s | 0.475s | **9.6x** | 13,300 | 0 | $0.0355 | $0.0000 | **100.0%** |
| Search and Filter Results | 2 | 4.69s | 0.170s | **27.6x** | 13,300 | 0 | $0.0355 | $0.0000 | **100.0%** |
| Multi-step Signup Form | 3 | 6.84s | 0.133s | **51.3x** | 19,950 | 0 | $0.0533 | $0.0000 | **100.0%** |
| E-commerce Product Page | 2 | 4.70s | 0.161s | **29.1x** | 13,300 | 0 | $0.0355 | $0.0000 | **100.0%** |
| Settings Toggle Options | 2 | 4.82s | 0.174s | **27.7x** | 13,300 | 0 | $0.0355 | $0.0000 | **100.0%** |
| Data Table Pagination | 2 | 4.65s | 0.139s | **33.4x** | 13,300 | 0 | $0.0355 | $0.0000 | **100.0%** |
| Support Ticket Submission | 3 | 6.77s | 0.133s | **51.1x** | 19,950 | 0 | $0.0533 | $0.0000 | **100.0%** |
| Fuzzy Search Navigation | 2 | 4.56s | 0.135s | **33.8x** | 13,300 | 0 | $0.0355 | $0.0000 | **100.0%** |
| Profile Update Flow | 3 | 6.86s | 0.166s | **41.4x** | 19,950 | 0 | $0.0533 | $0.0000 | **100.0%** |
| Complex Nested Form | 2 | 4.66s | 0.159s | **29.3x** | 13,300 | 0 | $0.0355 | $0.0000 | **100.0%** |
| **Total / Average** | - | **53.10s** | **1.845s** | **28.8x** | **152,950** | **0** | **$0.408** | **$0.000** | **100.00%** |
