// Mac で伝票の写真を文字にする（iPhone のショートカット「画像からテキストを抽出」と同じ Apple の文字認識）。
// 解析（index.html の parseOcrText）の調整用。写真は Mac の外に出ない。
//   使い方: swift tools/ocr-mac.swift samples/伝票.jpg > samples/ocr_伝票.txt
import Vision
import AppKit
let url = URL(fileURLWithPath: CommandLine.arguments[1])
let img = NSImage(contentsOf: url)!
let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil)!
let req = VNRecognizeTextRequest()
req.recognitionLevel = .accurate
req.recognitionLanguages = ["ja-JP"]
try VNImageRequestHandler(cgImage: cg).perform([req])
// 認識結果が返ってきた順そのまま（ショートカットの「画像からテキストを抽出」に近い形）
print((req.results ?? []).compactMap { $0.topCandidates(1).first?.string }.joined(separator: "\n"))
