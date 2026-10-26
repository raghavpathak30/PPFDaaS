#pragma once

// Minimal streaming JSON writer (objects, arrays, scalars). Enough for the
// result files; no escaping beyond what the harness's own strings need.

#include <cmath>
#include <cstdint>
#include <iomanip>
#include <ostream>
#include <sstream>
#include <string>
#include <vector>

class JsonWriter {
public:
    explicit JsonWriter(std::ostream &os) : os_(os) { os_ << std::setprecision(10); }

    JsonWriter &begin_object(const std::string &key = "") { open(key, '{'); return *this; }
    JsonWriter &end_object() { close('}'); return *this; }
    JsonWriter &begin_array(const std::string &key = "") { open(key, '['); return *this; }
    JsonWriter &end_array() { close(']'); return *this; }

    JsonWriter &str(const std::string &key, const std::string &v) { prefix(key); os_ << quote(v); return *this; }
    JsonWriter &num(const std::string &key, double v) {
        prefix(key);
        if (std::isfinite(v)) os_ << v; else os_ << "null";
        return *this;
    }
    JsonWriter &integer(const std::string &key, std::int64_t v) { prefix(key); os_ << v; return *this; }
    JsonWriter &boolean(const std::string &key, bool v) { prefix(key); os_ << (v ? "true" : "false"); return *this; }
    JsonWriter &null(const std::string &key) { prefix(key); os_ << "null"; return *this; }

    static std::string quote(const std::string &s) {
        std::string o = "\"";
        for (char c : s) {
            switch (c) {
                case '"': o += "\\\""; break;
                case '\\': o += "\\\\"; break;
                case '\n': o += "\\n"; break;
                case '\t': o += "\\t"; break;
                default: o += c;
            }
        }
        return o + "\"";
    }

private:
    void prefix(const std::string &key) {
        if (!first_.empty()) {
            if (!first_.back()) os_ << ",";
            first_.back() = false;
            os_ << "\n" << std::string(2 * first_.size(), ' ');
        }
        if (!key.empty()) os_ << quote(key) << ": ";
    }
    void open(const std::string &key, char c) { prefix(key); os_ << c; first_.push_back(true); }
    void close(char c) {
        const bool empty = first_.back();
        first_.pop_back();
        if (!empty) os_ << "\n" << std::string(2 * first_.size(), ' ');
        os_ << c;
        if (first_.empty()) os_ << "\n";
    }

    std::ostream &os_;
    std::vector<bool> first_;  // per open container: nothing written yet
};
