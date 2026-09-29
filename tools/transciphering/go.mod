module github.com/raghavpathak30/ppfdaas/transciphering

go 1.25.0

require (
	github.com/ldsec/lattigo/v2 v2.0.0-00010101000000-000000000000
	golang.org/x/crypto v0.55.0
)

require golang.org/x/sys v0.47.0 // indirect

replace github.com/ldsec/lattigo/v2 => ../../third_party/RtF-Transciphering
