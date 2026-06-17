.PHONY: build reproduce dry-run test clean

# Build all C++ targets (configure first if needed)
build:
	@if [ ! -f vendor_server/build/Makefile ]; then \
		cmake -B vendor_server/build -S vendor_server -DCMAKE_BUILD_TYPE=Release; \
	fi
	cmake --build vendor_server/build --parallel

# Run the full artifact-regeneration pipeline (requires build + data/creditcard.csv)
reproduce: build
	python3 scripts/reproduce_all.py

# Print the pipeline plan without executing anything
dry-run:
	python3 scripts/reproduce_all.py --dry-run

# Run C++ unit tests and Python structural verification
test:
	ctest --test-dir vendor_server/build --output-on-failure
	python3 tests/verify_all.py

# Remove build trees (binaries are reproduced by `make build`)
clean:
	rm -rf vendor_server/build build
