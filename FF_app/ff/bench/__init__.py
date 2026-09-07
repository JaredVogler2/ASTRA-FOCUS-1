"""ff.bench — benchmark-suite support package (B1 governing prompt, adapted).

Pure, deterministic fleet-mutation functions used by the scenario corpus in
``benchmarks/`` and the runner ``tools/bench.py``. Nothing in this package
touches the engine, services, or web layers — it only produces mutated
copies of a Fleet for the engine to schedule.
"""
