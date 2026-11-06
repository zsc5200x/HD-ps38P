// PS38 图形验证码 OCR（macOS Vision，可选）。
//
// 用法:
//   swift scripts/ocr_captcha.swift /path/to/captcha.jpg
//
// 输出: 识别出的 4 位数字（可能包含噪点字符；Python 侧只取最后 4 位数字）。
// 说明: 这是本机可用的兜底方案，不保证 100% 准确；调用方必须把
//       INVALID_CAPTCHA 视为“可安全重试”，并在多次失败后转人工。

import AppKit
import Foundation
import Vision

let arguments = CommandLine.arguments
guard arguments.count > 1 else {
    FileHandle.standardError.write(Data("usage: ocr_captcha.swift <image>\n".utf8))
    exit(2)
}
let path = arguments[1]
let scale = max(1.0, min(4.0, Double(arguments.count > 2 ? arguments[2] : "3") ?? 3.0))
guard let source = NSImage(contentsOfFile: path) else {
    FileHandle.standardError.write(Data("cannot open image\n".utf8))
    exit(1)
}
let width = max(1, Int(source.size.width * scale))
let height = max(1, Int(source.size.height * scale))
guard let bitmap = NSBitmapImageRep(
    bitmapDataPlanes: nil,
    pixelsWide: width,
    pixelsHigh: height,
    bitsPerSample: 8,
    samplesPerPixel: 4,
    hasAlpha: true,
    isPlanar: false,
    colorSpaceName: .deviceRGB,
    bytesPerRow: 0,
    bitsPerPixel: 0
) else {
    FileHandle.standardError.write(Data("cannot allocate bitmap\n".utf8))
    exit(1)
}
NSGraphicsContext.saveGraphicsState()
NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: bitmap)
NSGraphicsContext.current?.imageInterpolation = .high
source.draw(in: NSRect(x: 0, y: 0, width: width, height: height))
NSGraphicsContext.restoreGraphicsState()
guard let cgImage = bitmap.cgImage else {
    FileHandle.standardError.write(Data("cannot create cgimage\n".utf8))
    exit(1)
}

let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.usesLanguageCorrection = false
request.recognitionLanguages = ["en-US"]
request.customWords = ["0", "1", "2", "3", "4", "5", "6", "7", "8", "9"]
let handler = VNImageRequestHandler(cgImage: cgImage, options: [:])
do {
    try handler.perform([request])
} catch {
    FileHandle.standardError.write(Data("ocr failed\n".utf8))
    exit(1)
}
for observation in request.results ?? [] {
    if let candidate = observation.topCandidates(1).first {
        print(candidate.string)
    }
}
