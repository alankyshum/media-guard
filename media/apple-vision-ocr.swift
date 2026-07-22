import Foundation
import Vision
import ImageIO
import CoreGraphics

struct Detection: Encodable {
    let box: [Int]
    let position: Position
    let color: String
    let colorfulness: Double
    let geometry: Geometry
    let text: String
    let confidence: Double
    let ocrTool: String
}

struct Position: Encodable {
    let x: Double
    let y: Double
    let width: Double
    let height: Double
}

struct Geometry: Encodable {
    let areaFraction: Double
    let aspectRatio: Double
    let rectangleConfidence: Double
}

struct Result: Encodable {
    let status: String
    let text: String
    let confidence: Double
    let recognitionLevel: String
    let usesLanguageCorrection: Bool
    let revision: Int
    let latencyMs: Int
    let detections: [Detection]
    let imageSize: [Int]
    let error: String?
}

func intersectionOverUnion(_ lhs: [Int], _ rhs: [Int]) -> Double {
    let left = max(lhs[0], rhs[0])
    let top = max(lhs[1], rhs[1])
    let right = min(lhs[2], rhs[2])
    let bottom = min(lhs[3], rhs[3])
    let intersection = max(0, right - left) * max(0, bottom - top)
    let lhsArea = max(0, lhs[2] - lhs[0]) * max(0, lhs[3] - lhs[1])
    let rhsArea = max(0, rhs[2] - rhs[0]) * max(0, rhs[3] - rhs[1])
    let union = lhsArea + rhsArea - intersection
    return union == 0 ? 0 : Double(intersection) / Double(union)
}

func overlapOverSmaller(_ lhs: [Int], _ rhs: [Int]) -> Double {
    let left = max(lhs[0], rhs[0])
    let top = max(lhs[1], rhs[1])
    let right = min(lhs[2], rhs[2])
    let bottom = min(lhs[3], rhs[3])
    let intersection = max(0, right - left) * max(0, bottom - top)
    let lhsArea = max(0, lhs[2] - lhs[0]) * max(0, lhs[3] - lhs[1])
    let rhsArea = max(0, rhs[2] - rhs[0]) * max(0, rhs[3] - rhs[1])
    let smaller = min(lhsArea, rhsArea)
    return smaller == 0 ? 0 : Double(intersection) / Double(smaller)
}

let started = DispatchTime.now().uptimeNanoseconds

func emit(_ result: Result) {
    let encoder = JSONEncoder()
    encoder.outputFormatting = [.sortedKeys]
    FileHandle.standardOutput.write((try! encoder.encode(result) + Data([10])))
}

func result(status: String, text: String = "", confidence: Double = 0, detections: [Detection] = [], imageSize: [Int] = [], error: String? = nil) -> Result {
    Result(status: status, text: text, confidence: confidence, recognitionLevel: "accurate", usesLanguageCorrection: false, revision: VNRecognizeTextRequest.currentRevision, latencyMs: Int((DispatchTime.now().uptimeNanoseconds - started) / 1_000_000), detections: detections, imageSize: imageSize, error: error)
}

func colorName(_ rgb: (Double, Double, Double)) -> (String, Double) {
    let names: [(String, (Double, Double, Double))] = [
        ("red", (0.85, 0.18, 0.18)), ("orange", (0.90, 0.45, 0.12)),
        ("yellow", (0.90, 0.78, 0.12)), ("green", (0.20, 0.65, 0.25)),
        ("cyan", (0.12, 0.68, 0.72)), ("blue", (0.18, 0.38, 0.85)),
        ("purple", (0.55, 0.25, 0.72)), ("pink", (0.88, 0.35, 0.58))
    ]
    let scale = max(rgb.0 + rgb.1 + rgb.2, 1)
    let colorful = (max(rgb.0, max(rgb.1, rgb.2)) - min(rgb.0, min(rgb.1, rgb.2))) / scale
    if colorful < 0.12 { return ("neutral", colorful) }
    let best = names.min { lhs, rhs in
        let ld = pow(rgb.0 - lhs.1.0, 2) + pow(rgb.1 - lhs.1.1, 2) + pow(rgb.2 - lhs.1.2, 2)
        let rd = pow(rgb.0 - rhs.1.0, 2) + pow(rgb.1 - rhs.1.1, 2) + pow(rgb.2 - rhs.1.2, 2)
        return ld < rd
    }!
    return (best.0, colorful)
}

func averageColor(_ image: CGImage, crop: CGRect) -> (String, Double) {
    guard let provider = image.cropping(to: crop), let context = CGContext(data: nil, width: 1, height: 1, bitsPerComponent: 8, bytesPerRow: 4, space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue), let data = context.data else { return ("neutral", 0) }
    context.draw(provider, in: CGRect(x: 0, y: 0, width: 1, height: 1))
    let bytes = data.assumingMemoryBound(to: UInt8.self)
    return colorName((Double(bytes[0]) / 255, Double(bytes[1]) / 255, Double(bytes[2]) / 255))
}

func ocr(_ image: CGImage) throws -> (String, Double) {
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.usesLanguageCorrection = false
    request.recognitionLanguages = ["en-US"]
    try VNImageRequestHandler(cgImage: image, orientation: .up).perform([request])
    let lines = (request.results ?? []).compactMap { observation -> (String, Double)? in
        guard let candidate = observation.topCandidates(1).first else { return nil }
        return (candidate.string, Double(candidate.confidence))
    }
    return (lines.map(\.0).joined(separator: "\n"), lines.isEmpty ? 0 : lines.map(\.1).reduce(0, +) / Double(lines.count))
}

guard CommandLine.arguments.count == 2 else {
    emit(result(status: "error", error: "usage: apple-vision-ocr.swift IMAGE"))
    exit(2)
}

let path = CommandLine.arguments[1]
guard let source = CGImageSourceCreateWithURL(URL(fileURLWithPath: path) as CFURL, nil) else {
    emit(result(status: "error", error: "unable to decode image"))
    exit(1)
}
guard let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else {
    emit(result(status: "error", error: "unable to decode image"))
    exit(1)
}

let imageWidth = image.width
let imageHeight = image.height
let imageArea = Double(imageWidth) * Double(imageHeight)
let rectangleRequest = VNDetectRectanglesRequest()
rectangleRequest.minimumConfidence = 0.20
rectangleRequest.minimumSize = 0.04
rectangleRequest.minimumAspectRatio = 0.18
rectangleRequest.maximumAspectRatio = 5.5
rectangleRequest.maximumObservations = 32

do {
    try VNImageRequestHandler(cgImage: image, orientation: .up).perform([rectangleRequest])
    var detections: [Detection] = []
    for observation in rectangleRequest.results ?? [] {
        let normalized = observation.boundingBox
        let x = max(0, Int(floor(normalized.minX * Double(imageWidth))))
        let y = max(0, Int(floor((1 - normalized.maxY) * Double(imageHeight))))
        let width = min(imageWidth - x, max(1, Int(ceil(normalized.width * Double(imageWidth)))))
        let height = min(imageHeight - y, max(1, Int(ceil(normalized.height * Double(imageHeight)))))
        let areaFraction = (Double(width) * Double(height)) / imageArea
        let aspect = Double(width) / Double(max(height, 1))
        if areaFraction < 0.005 || areaFraction > 0.60 || aspect < 0.18 || aspect > 5.5 { continue }
        let crop = CGRect(x: x, y: y, width: width, height: height)
        guard let cropped = image.cropping(to: crop) else { continue }
        let (color, colorful) = averageColor(image, crop: crop)
        if colorful < 0.04 { continue }
        let (text, confidence) = try ocr(cropped)
        let box = [x, y, x + width, y + height]
        let position = Position(x: Double(x) / Double(imageWidth), y: Double(y) / Double(imageHeight), width: Double(width) / Double(imageWidth), height: Double(height) / Double(imageHeight))
        let geometry = Geometry(areaFraction: areaFraction, aspectRatio: aspect, rectangleConfidence: Double(observation.confidence))
        detections.append(Detection(box: box, position: position, color: color, colorfulness: colorful, geometry: geometry, text: text, confidence: confidence, ocrTool: "apple-vision"))
    }
    detections.sort { lhs, rhs in
        if lhs.geometry.rectangleConfidence != rhs.geometry.rectangleConfidence {
            return lhs.geometry.rectangleConfidence > rhs.geometry.rectangleConfidence
        }
        return lhs.geometry.areaFraction > rhs.geometry.areaFraction
    }
    var deduplicated: [Detection] = []
    for detection in detections where !deduplicated.contains(where: {
        intersectionOverUnion($0.box, detection.box) >= 0.55 || overlapOverSmaller($0.box, detection.box) >= 0.55
    }) {
        deduplicated.append(detection)
    }
    deduplicated.sort { ($0.box[1], $0.box[0]) < ($1.box[1], $1.box[0]) }
    emit(result(status: "ok", text: deduplicated.map(\.text).filter { !$0.isEmpty }.joined(separator: "\n\n"), confidence: deduplicated.isEmpty ? 0 : deduplicated.map(\.confidence).reduce(0, +) / Double(deduplicated.count), detections: deduplicated, imageSize: [imageWidth, imageHeight]))
} catch {
    emit(result(status: "error", imageSize: [imageWidth, imageHeight], error: String(describing: error)))
    exit(1)
}
