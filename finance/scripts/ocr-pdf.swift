// OCR a scanned PDF with the macOS Vision framework: text per page, on stdout.
// No third-party install (tesseract and friends are not on this Mac); runs with the
// CommandLineTools swift. German/Swiss contracts are the use (payroll's insurer sheets).
//
//     swift ocr-pdf.swift <in.pdf> [<first-page> [<last-page>]]  > out.txt
//
// Stdlib and macOS frameworks only, nothing private in here: paths come from the arguments.

import Foundation
import PDFKit
import Vision

guard CommandLine.arguments.count >= 2 else {
    FileHandle.standardError.write("usage: swift ocr-pdf.swift <in.pdf> [<first-page> [<last-page>]]\n".data(using: .utf8)!)
    exit(2)
}
let inPath = CommandLine.arguments[1]
let first = CommandLine.arguments.count > 2 ? Int(CommandLine.arguments[2]) ?? 1 : 1
let last = CommandLine.arguments.count > 3 ? Int(CommandLine.arguments[3]) ?? Int.max : Int.max

guard let doc = PDFDocument(url: URL(fileURLWithPath: inPath)) else {
    FileHandle.standardError.write("cannot open \(inPath)\n".data(using: .utf8)!)
    exit(1)
}

func ocr(_ cgImage: CGImage) -> String {
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.usesLanguageCorrection = true
    request.recognitionLanguages = ["de-CH", "en-US"]
    let handler = VNImageRequestHandler(cgImage: cgImage, options: [:])
    do {
        try handler.perform([request])
    } catch {
        FileHandle.standardError.write("vision failed: \(error)\n".data(using: .utf8)!)
        exit(1)
    }
    let lines = (request.results as? [VNRecognizedTextObservation])?.compactMap {
        $0.topCandidates(1).first?.string
    } ?? []
    return lines.joined(separator: "\n")
}

var out = ""
for index in 0..<doc.pageCount where index + 1 >= first && index + 1 <= last {
    guard let page = doc.page(at: index) else { continue }
    let bounds = page.bounds(for: .mediaBox)
    let scale: CGFloat = 300.0 / 72.0
    let size = CGSize(width: max(bounds.width, 1) * scale, height: max(bounds.height, 1) * scale)
    let image = page.thumbnail(of: size, for: .mediaBox)
    guard let cgImage = image.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
        FileHandle.standardError.write("page \(index + 1): no bitmap\n".data(using: .utf8)!)
        continue
    }
    out += "\n===== page \(index + 1) =====\n" + ocr(cgImage) + "\n"
    FileHandle.standardError.write("page \(index + 1)/\(doc.pageCount)\n".data(using: .utf8)!)
}
print(out, terminator: "")
