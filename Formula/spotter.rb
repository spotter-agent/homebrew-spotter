class Spotter < Formula
  include Language::Python::Virtualenv

  desc "Runtime trajectory supervision for coding agents"
  homepage "https://github.com/spotter-agent/spotter"
  url "https://github.com/spotter-agent/spotter/releases/download/v0.0.7/spotter_agent-0.0.7.tar.gz"
  sha256 "6ba6f148f5939a7dd96e5d558d1d36c67caef90bee29bf210cec1774a0857bcf"
  license "MIT"

  depends_on "python@3.14"

  resource "websockets" do
    url "https://files.pythonhosted.org/packages/da/ea/c0f7924f7ccf005d6ad1f829971762ae751727497d6db1977ba5a635314f/websockets-17.0.tar.gz"
    sha256 "6bbe83c4ef52a7533d2d8c6a3512b93722fd0db6bc6bc638d45edd49ef201444"
  end

  def install
    virtualenv_install_with_resources
  end

  service do
    run opt_bin/"spotterd"
    keep_alive path: opt_bin/"spotterd"
    process_type :background
    working_dir Dir.home
    environment_variables SPOTTER_HOME: "#{Dir.home}/.spotter"
  end

  test do
    ENV["SPOTTER_HOME"] = testpath/"state"
    payload = <<~JSON
      {"hook_event_name":"SessionStart","session_id":"homebrew-test"}
    JSON

    pipe_output("#{bin}/spotter hook", payload)

    assert_path_exists testpath/"state/sessions/homebrew-test.jsonl"
    assert_match "spotter #{version}", shell_output("#{bin}/spotter --version")
    assert_match "spotterd #{version}", shell_output("#{bin}/spotterd --version")
    refute_path_exists testpath/".codex"
  end
end
