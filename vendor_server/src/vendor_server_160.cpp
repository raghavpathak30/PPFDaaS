#include "inference_service_160.h"

#include <iostream>
#include <string>

int main(int argc, char **argv) {
    std::string weights_path = std::string(PPFDAAS_REPO_ROOT) + "/artifacts/model_weights.bin";
    int port = 50052;

    if (argc >= 2 && std::string(argv[1]).size() != 0) {
        weights_path = argv[1];
    }
    if (argc >= 3 && std::string(argv[2]).size() != 0) {
        port = std::stoi(argv[2]);
    }

    std::cout << "[Server-160] weights=" << weights_path << " port=" << port << "\n";
    return RunVendorServer160(weights_path, port);
}
