{ pkgs }: {
  description = "Python environment for Telegram bot";
  deps = [
    pkgs.python39
    pkgs.chromium
    pkgs.chromedriver
  ];
  env = {
    PYTHONBIN = "${pkgs.python39}/bin/python3.9";
    LANG = "en_US.UTF-8";
  };
} 