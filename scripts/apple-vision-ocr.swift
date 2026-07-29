import Foundation
import Vision
import ImageIO
import CryptoKit

struct Observation: Encodable { let text: String; let confidence: Double; let box: [Double] }
struct Output: Encodable {
    let status: String; let input: String; let mime: String?; let byte_size: Int64?; let sha256: String?
    let image_size: [Int]; let text: String; let confidence: Double; let recognition_level: String
    let revision: Int; let observations: [Observation]; let uncertainties: [String]; let error: String?
}

func emit(_ output: Output) {
    let encoder = JSONEncoder(); encoder.outputFormatting = [.sortedKeys]
    print(String(data: try! encoder.encode(output), encoding: .utf8)!)
}
func fail(_ path: String, _ message: String, _ code: Int32 = 1) -> Never {
    emit(Output(status: "error", input: path, mime: nil, byte_size: nil, sha256: nil, image_size: [], text: "", confidence: 0, recognition_level: "accurate", revision: VNRecognizeTextRequest.currentRevision, observations: [], uncertainties: [], error: message)); exit(code)
}
func sha256(_ data: Data) -> String {
    SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
}

let args = CommandLine.arguments
if args.contains("--check") { print("Apple Vision OCR available"); exit(0) }
guard args.count >= 2 else { fail("", "usage: apple-vision-ocr IMAGE [--format json|yaml]") }
let path = args[1]
guard ProcessInfo.processInfo.operatingSystemVersion.majorVersion >= 10 else { fail(path, "macOS with Vision is required") }
guard let data = FileManager.default.contents(atPath: path) else { fail(path, "input is not readable: \(path)") }
guard let source = CGImageSourceCreateWithData(data as CFData, nil), let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else { fail(path, "unable to decode image; PDF and unsupported formats must be rendered first") }
let request = VNRecognizeTextRequest(); request.recognitionLevel = .accurate; request.usesLanguageCorrection = false
do {
    try VNImageRequestHandler(cgImage: image, options: [:]).perform([request])
    let observations = (request.results ?? []).compactMap { item -> Observation? in
        guard let candidate = item.topCandidates(1).first else { return nil }
        let box = [Double(item.boundingBox.minX), Double(item.boundingBox.minY), Double(item.boundingBox.width), Double(item.boundingBox.height)]
        return Observation(text: candidate.string, confidence: Double(candidate.confidence), box: box)
    }
    let text = observations.map(\.text).joined(separator: "\n")
    let confidence = observations.isEmpty ? 0 : observations.map(\.confidence).reduce(0, +) / Double(observations.count)
    let output = Output(status: "ok", input: URL(fileURLWithPath: path).standardized.path, mime: CGImageSourceGetType(source).map { $0 as String }, byte_size: Int64(data.count), sha256: sha256(data), image_size: [image.width, image.height], text: text, confidence: confidence, recognition_level: "accurate", revision: VNRecognizeTextRequest.currentRevision, observations: observations, uncertainties: observations.isEmpty ? ["Vision detected no text"] : [], error: nil)
    emit(output)
} catch { fail(path, "Vision OCR failed: \(error)") }
