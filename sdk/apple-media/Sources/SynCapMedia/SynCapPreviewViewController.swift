import MobileVLCKit
import UIKit

public final class SynCapPreviewViewController: UIViewController, VLCMediaPlayerDelegate {
    private let urls: [URL]
    private let labels: [String]
    private let toolbar = UIView()
    private let countLabel = UILabel()
    private var tiles: [SynCapPreviewTile] = []
    private var players: [VLCMediaPlayer] = []
    private var readyPlayers = Set<ObjectIdentifier>()

    public init(urls: [URL], labels: [String]) throws {
        guard (1...8).contains(urls.count), labels.count == urls.count,
              urls.allSatisfy({ url in
                  ["rtsp", "tcp"].contains(url.scheme?.lowercased() ?? "")
                      && url.host != nil && url.user == nil && url.password == nil && url.fragment == nil
                      && (url.port == nil || (1...65535).contains(url.port!))
                      && (url.scheme?.lowercased() != "tcp" || url.port != nil)
              }) else {
            throw NSError(domain: "SynCapMedia", code: 1, userInfo: [NSLocalizedDescriptionKey: "Invalid camera preview endpoints or labels"])
        }
        self.urls = urls
        self.labels = labels
        super.init(nibName: nil, bundle: nil)
        modalPresentationStyle = .fullScreen
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { nil }

    public override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .black
        UIApplication.shared.isIdleTimerDisabled = true
        buildToolbar()
        buildPlayers()
    }

    private func buildToolbar() {
        toolbar.backgroundColor = UIColor(red: 0.02, green: 0.02, blue: 0.02, alpha: 0.98)
        view.addSubview(toolbar)

        let title = UILabel()
        title.text = "SynCap  ·  LIVE"
        title.textColor = SynCapPreviewTile.gold
        title.font = .systemFont(ofSize: 18, weight: .semibold)
        toolbar.addSubview(title)

        countLabel.textColor = .lightGray
        countLabel.font = .systemFont(ofSize: 13, weight: .medium)
        countLabel.textAlignment = .right
        toolbar.addSubview(countLabel)

        let close = UIButton(type: .system)
        close.setTitle("关闭", for: .normal)
        close.setTitleColor(SynCapPreviewTile.gold, for: .normal)
        close.titleLabel?.font = .systemFont(ofSize: 15, weight: .semibold)
        close.backgroundColor = UIColor(red: 0.09, green: 0.08, blue: 0.05, alpha: 1)
        close.layer.cornerRadius = 7
        close.addTarget(self, action: #selector(closePreview), for: .touchUpInside)
        toolbar.addSubview(close)

        title.tag = 101
        close.tag = 102
        updateCount()
    }

    private func buildPlayers() {
        let library = VLCLibrary.shared()
        library.setHumanReadableName("SynCap Studio", withHTTPUserAgent: "SynCap-Studio/1.0")

        for (index, url) in urls.enumerated() {
            let tile = SynCapPreviewTile(label: labels.indices.contains(index) ? labels[index] : "CAM \(index)")
            view.addSubview(tile)
            tiles.append(tile)

            let media = VLCMedia(url: url)
            if url.scheme?.lowercased() == "tcp" {
                media.addOption(":demux=hevc")
                media.addOption(":hevc-fps=29.4118")
            } else {
                media.addOption(":rtsp-tcp")
            }
            media.addOption(":network-caching=120")
            media.addOption(":clock-jitter=0")
            media.addOption(":clock-synchro=0")

            let player = VLCMediaPlayer(library: library)
            player.delegate = self
            player.drawable = tile.videoView
            player.media = media
            players.append(player)
            player.play()
        }
    }

    public override func viewDidLayoutSubviews() {
        super.viewDidLayoutSubviews()
        let safe = view.safeAreaInsets
        let toolbarHeight: CGFloat = 56
        toolbar.frame = CGRect(x: 0, y: safe.top, width: view.bounds.width, height: toolbarHeight)
        toolbar.viewWithTag(101)?.frame = CGRect(x: 18, y: 0, width: 180, height: toolbarHeight)
        toolbar.viewWithTag(102)?.frame = CGRect(x: toolbar.bounds.width - 84, y: 9, width: 72, height: 38)
        countLabel.frame = CGRect(x: max(198, toolbar.bounds.width - 260), y: 0, width: 160, height: toolbarHeight)

        let gap: CGFloat = 6
        let top = safe.top + toolbarHeight + gap
        let availableHeight = max(1, view.bounds.height - top - safe.bottom - gap)
        let columns = urls.count == 1 ? 1 : 2
        let rows = Int(ceil(Double(urls.count) / Double(columns)))
        let tileWidth = (view.bounds.width - gap * CGFloat(columns + 1)) / CGFloat(columns)
        let tileHeight = (availableHeight - gap * CGFloat(rows - 1)) / CGFloat(rows)

        for (index, tile) in tiles.enumerated() {
            let row = index / columns
            let column = index % columns
            tile.frame = CGRect(
                x: gap + CGFloat(column) * (tileWidth + gap),
                y: top + CGFloat(row) * (tileHeight + gap),
                width: tileWidth,
                height: tileHeight
            )
        }
    }

    public func mediaPlayerStateChanged(_ notification: Notification) {
        guard let player = notification.object as? VLCMediaPlayer,
              let index = players.firstIndex(where: { $0 === player }) else { return }
        DispatchQueue.main.async {
            let identifier = ObjectIdentifier(player)
            switch player.state {
            case .playing:
                self.readyPlayers.insert(identifier)
                self.tiles[index].setState("● 实时", color: UIColor(red: 0.24, green: 0.81, blue: 0.38, alpha: 1))
            case .opening, .buffering:
                self.readyPlayers.remove(identifier)
                self.tiles[index].setState("连接中", color: .lightGray)
            case .error, .ended, .stopped:
                self.readyPlayers.remove(identifier)
                self.tiles[index].setState("连接失败", color: UIColor(red: 0.94, green: 0.38, blue: 0.38, alpha: 1))
            default:
                break
            }
            self.updateCount()
        }
    }

    private func updateCount() {
        countLabel.text = "\(readyPlayers.count) / \(urls.count) 路已连接"
        countLabel.textColor = readyPlayers.isEmpty ? .lightGray : UIColor(red: 0.24, green: 0.81, blue: 0.38, alpha: 1)
    }

    @objc private func closePreview() {
        players.forEach { $0.stop() }
        dismiss(animated: true)
    }

    public override func viewDidDisappear(_ animated: Bool) {
        super.viewDidDisappear(animated)
        players.forEach { $0.stop() }
        UIApplication.shared.isIdleTimerDisabled = false
    }
}

private final class SynCapPreviewTile: UIView {
    static let gold = UIColor(red: 0.88, green: 0.74, blue: 0.36, alpha: 1)
    let videoView = UIView()
    private let nameLabel = UILabel()
    private let stateLabel = UILabel()

    init(label: String) {
        super.init(frame: .zero)
        backgroundColor = .black
        layer.borderWidth = 1
        layer.borderColor = UIColor(red: 0.38, green: 0.30, blue: 0.14, alpha: 1).cgColor
        layer.cornerRadius = 8
        clipsToBounds = true

        videoView.backgroundColor = .black
        addSubview(videoView)

        nameLabel.text = label
        nameLabel.textColor = Self.gold
        nameLabel.font = .systemFont(ofSize: 13, weight: .semibold)
        nameLabel.backgroundColor = UIColor(white: 0, alpha: 0.72)
        addSubview(nameLabel)

        stateLabel.text = "连接中"
        stateLabel.textColor = .lightGray
        stateLabel.textAlignment = .right
        stateLabel.font = .systemFont(ofSize: 12, weight: .semibold)
        stateLabel.backgroundColor = UIColor(white: 0, alpha: 0.72)
        addSubview(stateLabel)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { nil }

    override func layoutSubviews() {
        super.layoutSubviews()
        videoView.frame = bounds
        nameLabel.frame = CGRect(x: 0, y: 0, width: bounds.width * 0.62, height: 34)
        stateLabel.frame = CGRect(x: bounds.width * 0.62, y: 0, width: bounds.width * 0.38, height: 34)
        nameLabel.layoutMargins = UIEdgeInsets(top: 0, left: 10, bottom: 0, right: 4)
    }

    func setState(_ text: String, color: UIColor) {
        stateLabel.text = text
        stateLabel.textColor = color
    }
}
