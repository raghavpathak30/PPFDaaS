#include "inference_service_160.h"

#include <exception>
#include <iostream>
#include <string>

int main(int argc, char **argv) {
    try {
        std::string weights_path = std::string(PPFDAAS_REPO_ROOT) + "/artifacts/model_weights.bin";
        int port = 50052;

        if (argc >= 2 && std::string(argv[1]).size() != 0) {
            weights_path = argv[1];
        }
        if (argc >= 3 && std::string(argv[2]).size() != 0) {
            port = std::stoi(argv[2]);  // throws std::invalid_argument on non-numeric -> caught below
        }

        std::cout << "[Server-160] weights=" << weights_path << " port=" << port << "\n";
        return RunVendorServer160(weights_path, port);
    } catch (const std::exception &e) {
        // Clean fail-closed exit. Without this, a throw from CKKSContext160 (e.g. missing
        // Galois keys) would escape main -> std::terminate -> SIGABRT (exit 134, core dump).
        // exit(1) makes the compose healthcheck stay unhealthy and the client's
        // depends_on:service_healthy block -- the deploy fails loudly instead of serving garbage.
        std::cerr << "[Server-160] FATAL: " << e.what() << "\n";
        return 1;
    }
}