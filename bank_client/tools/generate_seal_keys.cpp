#include "ckks_context.h"

#include <filesystem>
#include <fstream>
#include <iostream>
#include <stdexcept>

namespace fs = std::filesystem;

int main() {
    const fs::path repo_root = fs::path(PPFDAAS_REPO_ROOT);
    const fs::path artifacts_dir = repo_root / "artifacts";
    fs::create_directories(artifacts_dir);

    CKKSContext ctx;

    {
        std::ofstream public_out(artifacts_dir / "public_key.bin", std::ios::binary);
        if (!public_out.is_open()) {
            throw std::runtime_error("failed to open artifacts/public_key.bin for writing");
        }
        ctx.public_key.save(public_out);
    }

    {
        std::ofstream secret_out(artifacts_dir / "secret_key.bin", std::ios::binary);
        if (!secret_out.is_open()) {
            throw std::runtime_error("failed to open artifacts/secret_key.bin for writing");
        }
        ctx.secret_key.save(secret_out);
    }

    {
        std::ofstream galois_out(artifacts_dir / "galois_keys.bin", std::ios::binary);
        if (!galois_out.is_open()) {
            throw std::runtime_error("failed to open artifacts/galois_keys.bin for writing");
        }
        ctx.galois_keys.save(galois_out);
    }

    std::cout << "[keys] wrote " << (artifacts_dir / "public_key.bin") << "\n";
    std::cout << "[keys] wrote " << (artifacts_dir / "secret_key.bin") << "\n";
    std::cout << "[keys] wrote " << (artifacts_dir / "galois_keys.bin") << "\n";
    return 0;
}
