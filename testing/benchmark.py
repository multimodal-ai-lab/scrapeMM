import asyncio
import time
from pathlib import Path

from scrapemm.retrieval import retrieve


async def main():
    # Load URLs from test_urls.txt
    urls_file = Path(__file__).parent / "test_urls.txt"
    urls = [line.strip() for line in urls_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    print(f"Loaded {len(urls)} URLs from {urls_file.name}")

    # Measure total retrieval time
    total_start = time.time()
    results = await retrieve(urls, prioritize="speed")
    assert isinstance(results, list)
    total_time = time.time() - total_start
    
    # Print summary
    successful = sum(1 for r in results if r.successful)
    print(f"\n{'=' * 80}")
    print(f"Total time:       {total_time:.2f}s")
    print(f"URLs retrieved:   {successful}/{len(results)}")
    avg_time = sum(r.retrieval_time for r in results) / len(results)
    print(f"Avg time per URL: {avg_time:.2f}s")
    max_time = max(r.retrieval_time for r in results)
    print(f"Max time:         {max_time:.2f}s")

    # Average retrieval time by method
    from collections import defaultdict
    method_times = defaultdict(list)
    for r in results:
        method_times[r.method or 'N/A'].append(r.retrieval_time)
    print(f"\nAverage retrieval time by method:")
    for method, times in sorted(method_times.items()):
        print(f"  {method:15s} | avg {sum(times)/len(times):6.2f}s | count {len(times)}")
    print(f"{'=' * 80}\n")

    # Print per-URL results sorted by retrieval time (desc)
    sorted_results = sorted(results, key=lambda r: r.retrieval_time, reverse=True)
    for response in sorted_results:
        status = "OK" if response.successful else "FAIL"
        errors_str = ""
        if response.errors:
            errors_str = " | Errors: " + "; ".join(f"{k}: {v}" for k, v in response.errors.items())
        print(f"[{status}] {response.retrieval_time:6.2f}s | {response.method or 'N/A':15s} | {response.url}{errors_str}")


if __name__ == "__main__":
    asyncio.run(main())
