#!/usr/bin/env ruby
# Registers the home-screen widget with the Xcode project: the extension target
# (de.buergerwecker.app.widget), the app's three Swift files for it (the two
# plugins and the view controller that registers them), the embed phase, the
# entitlements (App Group, keychain group), and the Release signing the runner uses.
# Idempotent. Run it after regenerating ios/ (`npx cap add ios`), which knows
# nothing of the widget. Needs the xcodeproj gem: `gem install --user-install xcodeproj`.
#
#   ruby ios/add-widget-target.rb
require "xcodeproj"

ROOT = File.expand_path("App", __dir__)
proj = Xcodeproj::Project.open(File.join(ROOT, "App.xcodeproj"))
app = proj.targets.find { |t| t.name == "App" } or abort "no App target"
app_group = proj.main_group["App"] or abort "no App group"
NAME = "BuergerweckerWidget"
BUNDLE = "de.buergerwecker.app.widget"

def add_file(group, target, rel, kind: :source)
  ref = group.files.find { |f| f.path == rel } || group.new_file(rel)
  phase = kind == :source ? target.source_build_phase : target.resources_build_phase
  phase.add_file_reference(ref, true) unless phase.files_references.include?(ref)
  ref
end

def add_variant(group, target, name, langs)
  vg = group.children.find { |c| c.is_a?(Xcodeproj::Project::Object::PBXVariantGroup) && c.name == name }
  vg ||= group.new_variant_group(name)
  langs.each do |lang|
    rel = "#{lang}.lproj/#{name}"
    unless vg.files.any? { |f| f.path == rel }
      f = vg.new_reference(rel)
      f.name = lang
    end
  end
  target.resources_build_phase.add_file_reference(vg, true) unless target.resources_build_phase.files_references.include?(vg)
end

# The app's side: the two plugins (the widget's list, the device credential in
# the shared Keychain group) and the view controller that registers them.
%w[WidgetBridgePlugin.swift SecureStorePlugin.swift MainViewController.swift].each { |f| add_file(app_group, app, f) }
app_group.new_file("App.entitlements") unless app_group.files.any? { |f| f.path == "App.entitlements" }
app.build_configurations.each { |c| c.build_settings["CODE_SIGN_ENTITLEMENTS"] = "App/App.entitlements" }

# The extension target.
widget = proj.targets.find { |t| t.name == NAME }
unless widget
  widget = proj.new_target(:app_extension, NAME, :ios, "18.0")
  proj.main_group.new_group(NAME, NAME)
  wgroup = proj.main_group[NAME]
  wgroup.new_file("Info.plist")
  wgroup.new_file("#{NAME}.entitlements")
  widget.frameworks_build_phase.add_file_reference(proj.frameworks_group.new_file("System/Library/Frameworks/WidgetKit.framework", :sdk_root))
  widget.frameworks_build_phase.add_file_reference(proj.frameworks_group.new_file("System/Library/Frameworks/SwiftUI.framework", :sdk_root))
  widget.build_configurations.each do |c|
    c.build_settings.merge!(
      "PRODUCT_BUNDLE_IDENTIFIER" => BUNDLE,
      "PRODUCT_NAME" => NAME,
      "INFOPLIST_FILE" => "#{NAME}/Info.plist",
      "GENERATE_INFOPLIST_FILE" => "NO",
      "CODE_SIGN_ENTITLEMENTS" => "#{NAME}/#{NAME}.entitlements",
      "SWIFT_VERSION" => "5.0",
      "TARGETED_DEVICE_FAMILY" => "1,2",
      "IPHONEOS_DEPLOYMENT_TARGET" => "18.0",
      "MARKETING_VERSION" => "1.0",
      "CURRENT_PROJECT_VERSION" => "1",
      "SKIP_INSTALL" => "YES",
      "LD_RUNPATH_SEARCH_PATHS" => "$(inherited) @executable_path/Frameworks @executable_path/../../Frameworks",
      "ASSETCATALOG_COMPILER_GLOBAL_ACCENT_COLOR_NAME" => "",
      "ASSETCATALOG_COMPILER_WIDGET_BACKGROUND_COLOR_NAME" => "",
    )
  end
  app.add_dependency(widget)
  embed = app.copy_files_build_phases.find { |p| p.name == "Embed Foundation Extensions" } ||
          app.new_copy_files_build_phase("Embed Foundation Extensions")
  embed.dst_subfolder_spec = "13" # PlugIns
  embed.dst_path = ""
  bf = embed.add_file_reference(widget.product_reference, true)
  bf.settings = { "ATTRIBUTES" => ["RemoveHeadersOnCopy"] }
end

# Outside the block above on purpose: files added later register on every run.
wgroup = proj.main_group[NAME] or abort "no #{NAME} group"
add_file(wgroup, widget, "#{NAME}.swift")
add_file(wgroup, widget, "PrivacyInfo.xcprivacy", kind: :resource)
add_variant(wgroup, widget, "Localizable.strings", %w[de en])

# Release is what the runner archives for TestFlight: signed by hand with the
# distribution certificate and the profiles ios/asc.mjs fetches under these
# names (profileName() there). Debug stays automatic.
[app, widget].each do |t|
  release = t.build_configurations.find { |c| c.name == "Release" }
  release.build_settings.merge!(
    "CODE_SIGN_STYLE" => "Manual",
    "CODE_SIGN_IDENTITY" => "Apple Distribution",
    "PROVISIONING_PROFILE_SPECIFIER" => "Buergerwecker CI #{release.build_settings["PRODUCT_BUNDLE_IDENTIFIER"]}",
  )
end

# The shared "App" scheme (none is checked in; xcodebuild picks the project's
# autocreated one). Left alone: building scheme App builds the widget through
# the target dependency.
proj.save
puts "ok: #{proj.targets.map(&:name).join(', ')}"
