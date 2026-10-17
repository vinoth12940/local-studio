// Local PDF/image reading for chat. Vision OCR never sends document contents off this Mac.
import AppKit
import PDFKit
import Vision

func recognize(_ image: CGImage) throws -> String {
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.usesLanguageCorrection = false
    request.recognitionLanguages = ["en-US"]
    try VNImageRequestHandler(cgImage: image).perform([request])
    let observations = (request.results ?? []).sorted { $0.boundingBox.midY > $1.boundingBox.midY }
    var rows: [[VNRecognizedTextObservation]] = []
    for observation in observations {
        if let last = rows.last, let first = last.first, abs(first.boundingBox.midY - observation.boundingBox.midY) < 0.012 {
            rows[rows.count - 1].append(observation)
        } else { rows.append([observation]) }
    }
    return rows.map { row in
        row.sorted { $0.boundingBox.minX < $1.boundingBox.minX }.compactMap { $0.topCandidates(1).first?.string }.joined(separator: " | ")
    }.joined(separator: "\n")
}

func imageText(_ image: NSImage) throws -> String {
    guard let cgImage = image.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
        throw NSError(domain: "DocumentReader", code: 1, userInfo: [NSLocalizedDescriptionKey: "Cannot render this page"])
    }
    return try recognize(cgImage)
}

do {
    guard CommandLine.arguments.count == 4,
          let start = Int(CommandLine.arguments[2]), let count = Int(CommandLine.arguments[3]),
          start > 0, (1...4).contains(count) else {
        throw NSError(domain: "DocumentReader", code: 2, userInfo: [NSLocalizedDescriptionKey: "Usage: DocumentReader local-file start-page page-count (1-4)"])
    }
    let url = URL(fileURLWithPath: CommandLine.arguments[1])
    var pages: [[String: Any]] = []
    let total: Int
    if url.pathExtension.lowercased() == "pdf" {
        guard let document = PDFDocument(url: url), !document.isLocked else {
            throw NSError(domain: "DocumentReader", code: 3, userInfo: [NSLocalizedDescriptionKey: "PDF is unreadable or locked"])
        }
        total = document.pageCount
        guard start <= total else { throw NSError(domain: "DocumentReader", code: 4, userInfo: [NSLocalizedDescriptionKey: "Page is outside this document"]) }
        for index in (start - 1)..<min(total, start - 1 + count) {
            guard let page = document.page(at: index) else { continue }
            var text = page.string ?? ""
            let fields: [[String: String]] = page.annotations.compactMap { annotation in
                guard let value = annotation.widgetStringValue, !value.isEmpty, value != "Off", value != "/Off" else { return nil }
                return ["name": annotation.fieldName ?? "field", "value": value]
            }
            var method = "pdf_text"
            // Filled form values are often absent from page.string. Render them with their labels.
            if text.trimmingCharacters(in: .whitespacesAndNewlines).count < 80 || !fields.isEmpty {
                let bounds = page.bounds(for: .mediaBox)
                let scale = 2000 / max(bounds.width, bounds.height)
                let image = page.thumbnail(of: NSSize(width: bounds.width * scale, height: bounds.height * scale), for: .mediaBox)
                text = try imageText(image)
                method = "on_device_ocr"
            }
            if !fields.isEmpty {
                text += "\n\nFilled PDF fields:\n" + fields.map { "\($0["name"] ?? "field"): \($0["value"] ?? "")" }.joined(separator: "\n")
            }
            pages.append(["page": index + 1, "text": text, "method": method, "filled_fields": fields.count])
        }
    } else {
        guard start == 1, let image = NSImage(contentsOf: url) else {
            throw NSError(domain: "DocumentReader", code: 5, userInfo: [NSLocalizedDescriptionKey: "Image is unreadable or page is outside this image"])
        }
        total = 1
        pages.append(["page": 1, "text": try imageText(image), "method": "on_device_ocr", "filled_fields": 0])
    }
    let output: [String: Any] = ["pages_total": total, "pages": pages, "has_more_pages": start - 1 + pages.count < total]
    let data = try JSONSerialization.data(withJSONObject: output, options: [.sortedKeys])
    FileHandle.standardOutput.write(data)
} catch {
    let data = try! JSONSerialization.data(withJSONObject: ["error": error.localizedDescription])
    FileHandle.standardOutput.write(data)
    exit(1)
}
